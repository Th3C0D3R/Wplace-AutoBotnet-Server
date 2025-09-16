"""Módulo de orquestación de sesiones.

Este módulo maneja la lógica compleja de sesiones de trabajo, incluyendo
la orquestación automática de reparaciones, distribución de trabajo entre slaves,
y gestión del ciclo de vida de las sesiones.

Funcionalidades:
- Orquestación automática de sesiones con bucles continuos
- Distribución inteligente de trabajo basada en cargas de slaves
- Filtrado y priorización de cambios según configuración Guard
- Manejo de reintentos y reasignación de lotes fallidos
- Control de sesiones (start, pause, stop, one-batch)
"""

import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Any
from collections import defaultdict
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
import logging

try:
    # Importaciones relativas
    from .models import SessionLocal, SessionModel
    from .storage import (
        connected_slaves, active_sessions, active_projects, guard_config,
        active_protect_loops, batch_tracker, _last_preview_timestamp,
        is_locked_change
    )
    from .connection_manager import manager
    from .pixel_patterns import select_pixels_by_pattern
except ImportError:
    # Importaciones absolutas
    from models import SessionLocal, SessionModel
    from storage import (
        connected_slaves, active_sessions, active_projects, guard_config,
        active_protect_loops, batch_tracker, _last_preview_timestamp,
        is_locked_change
    )
    from connection_manager import manager
    from pixel_patterns import select_pixels_by_pattern

logger = logging.getLogger(__name__)


def setup_session_endpoints(app):
    """Configurar endpoints de sesiones en la aplicación FastAPI."""
    
    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str):
        """Iniciar una sesión de trabajo con orquestación automática."""
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session = active_sessions[session_id]
        project = active_projects.get(session.project_id)
        
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
            
        # Preparar slaves con modo y proyecto
        valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
        if not valid_slaves:
            raise HTTPException(status_code=400, detail="No valid slaves in session")
            
        for slave_id in valid_slaves:
            await manager.send_to_slave(slave_id, {"type": "setMode", "mode": project.mode})
            await manager.send_to_slave(slave_id, {"type": "loadProject", "config": project.config})
        
        # Lanzar bucle continuo en segundo plano
        active_protect_loops[session_id] = {"running": True}
        
        # Actualizar estado en DB
        db = SessionLocal()
        try:
            s = db.query(SessionModel).filter(SessionModel.id == session_id).first()
            if s:
                s.status = 'running'
                s.updated_at = datetime.utcnow()
                db.commit()
        except SQLAlchemyError as e:
            logger.error(f"DB update session running error: {e}")
            db.rollback()
        finally:
            db.close()
        
        # Función de filtrado de cambios
        async def filter_changes(preview_data: Dict[str, Any]) -> List[Dict[str, Any]]:
            changes = preview_data.get('changes', []) if isinstance(preview_data, dict) else []
            # Elegibles para reparación: missing, absent e incorrect
            changes = [c for c in changes if c.get('type') in ('missing', 'absent', 'incorrect')]
            
            # Aplicar filtros de color de guard_config
            excluded_ids = set(guard_config.get('excludedColorIds') or []) if guard_config.get('excludeColor') else set()
            preferred_ids = set(guard_config.get('preferredColorIds') or []) if guard_config.get('preferColor') else set()
            
            def exp_color(c):
                return c.get('expectedColor', c.get('color', 0))
                
            changes = [c for c in changes if exp_color(c) not in excluded_ids]
            
            # Prioridad: missing primero y preferidos primero
            def prio(c):
                col = exp_color(c)
                # Tratar incorrect igual que missing en prioridad
                is_missing_or_incorrect = 0 if c.get('type') in ('missing', 'incorrect') else 1
                is_pref = 0 if col in preferred_ids else 1
                return (is_missing_or_incorrect, is_pref)
                
            changes.sort(key=prio)
            return changes
        
        # Bucle de orquestación
        async def orchestrate_loop():
            try:
                while active_protect_loops.get(session_id, {}).get('running'):
                    try:
                        # 1. Slaves válidos
                        current_valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
                        if not current_valid_slaves:
                            await asyncio.sleep(3)
                            continue
                        
                        # 2. Preview del favorito (forzar check)
                        fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
                        if fav_id:
                            old_ts = _last_preview_timestamp.get(fav_id, 0)
                            await manager.send_to_slave(fav_id, {"type": "guardControl", "action": "check"})
                            for _ in range(20):
                                await asyncio.sleep(0.25)
                                if _last_preview_timestamp.get(fav_id, 0) > old_ts:
                                    break
                                    
                        fav = connected_slaves.get(fav_id) if fav_id else None
                        preview = (fav.telemetry.get('preview_data') if fav and isinstance(fav.telemetry, dict) else None) or {}
                        changes = await filter_changes(preview)
                        
                        if not isinstance(changes, list):
                            changes = []
                        else:
                            bad = [c for c in changes if not isinstance(c, dict)]
                            if bad:
                                logger.warning(f"[orchestrate_loop] Ignorando {len(bad)} cambios no dict")
                                changes = [c for c in changes if isinstance(c, dict)]
                        
                        # 3. Cargas
                        charges: Dict[str, int] = {}
                        total_remaining = 0
                        for sid in current_valid_slaves:
                            try:
                                rem = int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
                            except Exception:
                                rem = 0
                            charges[sid] = rem
                            total_remaining += rem
                        
                        if not changes:
                            await asyncio.sleep(5)
                            continue
                        if total_remaining <= 0:
                            await asyncio.sleep(30)
                            continue
                        
                        # 4. Planificación
                        pixels_per_batch = int(guard_config.get('pixelsPerBatch') or 10)
                        spend_all = bool(guard_config.get('spendAllPixelsOnStart'))
                        round_total = sum(charges.values()) if spend_all else min(sum(charges.values()), pixels_per_batch)
                        if round_total <= 0:
                            await asyncio.sleep(5)
                            continue
                        
                        plan: Dict[str, int] = {sid: 0 for sid in current_valid_slaves}
                        order = [sid for sid in current_valid_slaves if charges.get(sid, 0) > 0]
                        idx = 0
                        assigned = 0
                        
                        while assigned < round_total and order:
                            sid = order[idx % len(order)]
                            if plan[sid] < charges[sid]:
                                plan[sid] += 1
                                assigned += 1
                            idx += 1
                            if all(plan[s] >= charges[s] for s in order) and assigned < round_total:
                                break
                        
                        try:
                            changes = [ch for ch in changes if not is_locked_change(ch)]
                        except Exception:
                            pass
                            
                        pick = min(len(changes), sum(plan.values()))
                        if pick <= 0:
                            await asyncio.sleep(5)
                            continue
                        
                        try:
                            selected = select_pixels_by_pattern(
                                str(guard_config.get('protectionPattern', 'random')), 
                                changes, 
                                pick
                            )
                        except Exception:
                            selected = changes[:pick]
                        
                        # 5. Agrupar y construir colas
                        TILE = 1000
                        queues: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in current_valid_slaves}
                        rr_list = []
                        
                        for sid in [s for s in current_valid_slaves if plan.get(s, 0) > 0]:
                            rr_list += [sid] * plan[sid]
                            
                        for i, ch in enumerate(selected):
                            if not isinstance(ch, dict):
                                continue
                            sid = rr_list[i]
                            queues[sid].append(ch)
                        
                        req_id = uuid.uuid4().hex
                        batch_tracker.create(req_id)
                        
                        async def send_sub(slave_id: str, items: List[dict]):
                            subtile: Dict[tuple, List[dict]] = defaultdict(list)
                            for ch in items:
                                if not isinstance(ch, dict):
                                    continue
                                try:
                                    x = int(ch.get('x'))
                                    y = int(ch.get('y'))
                                except Exception:
                                    continue
                                subtile[(x // TILE, y // TILE)].append(ch)
                            
                            SUB = 40
                            for (tx, ty), lst in subtile.items():
                                for i in range(0, len(lst), SUB):
                                    part = lst[i:i+SUB]
                                    coords = [{'x': it['x'], 'y': it['y']} for it in part]
                                    colors = [int(it.get('expectedColor', it.get('color', 0))) for it in part]
                                    payload = {
                                        'tileX': tx, 
                                        'tileY': ty, 
                                        'coords': coords, 
                                        'colors': colors, 
                                        'requestId': req_id
                                    }
                                    batch_tracker.assign(req_id, slave_id, payload, 0)
                                    await manager.send_to_slave(slave_id, {'type': 'paintBatch', **payload})
                        
                        for sid, items in queues.items():
                            if items:
                                await send_sub(sid, items)
                        
                        # Esperar resultados con reintentos
                        deadline = asyncio.get_event_loop().time() + 90.0
                        while asyncio.get_event_loop().time() < deadline:
                            await asyncio.sleep(0.3)
                            if batch_tracker.get_pending(req_id) == 0:
                                break
                                
                            fails = batch_tracker.failed_assignments(req_id)
                            for (sid, key), data in fails:
                                candidates = (
                                    [x for x in current_valid_slaves if x != sid and charges.get(x, 0) > 0] or
                                    [x for x in current_valid_slaves if x != sid]
                                )
                                if not candidates:
                                    candidates = current_valid_slaves
                                    
                                idx = (idx + 1) if isinstance(idx, int) else 0
                                new_sid = candidates[idx % len(candidates)]
                                attempts = batch_tracker.inc_attempts(req_id, sid, key)
                                
                                if attempts <= 3:
                                    await manager.send_to_slave(new_sid, {
                                        'type': 'paintBatch',
                                        'tileX': data['tileX'],
                                        'tileY': data['tileY'],
                                        'coords': data['coords'],
                                        'colors': data['colors'],
                                        'requestId': req_id
                                    })
                        
                        await asyncio.sleep(1)
                        
                    except Exception as loop_iteration_err:
                        logger.error(f"orchestrate_loop iteration error: {loop_iteration_err}")
                        await asyncio.sleep(2)
                        continue
                        
            except Exception as e:
                logger.error(f"orchestrate_loop error: {e}")
        
        # Ejecutar en background
        asyncio.create_task(orchestrate_loop())
        
        # Responder con sumatorio de cargas actuales
        total_remaining = 0
        for sid in valid_slaves:
            try:
                total_remaining += int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
            except Exception:
                pass
                
        return {"status": "started", "session_id": session_id, "total_remaining": total_remaining}
    
    @app.post("/api/sessions/{session_id}/pause")
    async def pause_session(session_id: str):
        """Pausar una sesión de trabajo."""
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session = active_sessions[session_id]
        
        for slave_id in session.slave_ids:
            if slave_id in connected_slaves:
                await manager.send_to_slave(slave_id, {
                    "type": "control",
                    "action": "pause"
                })
        
        # Actualizar estado en DB
        db = SessionLocal()
        try:
            s = db.query(SessionModel).filter(SessionModel.id == session_id).first()
            if s:
                s.status = 'paused'
                s.updated_at = datetime.utcnow()
                db.commit()
        except SQLAlchemyError as e:
            logger.error(f"DB update session paused error: {e}")
            db.rollback()
        finally:
            db.close()
            
        return {"status": "paused", "session_id": session_id}
    
    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str):
        """Detener una sesión de trabajo."""
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session = active_sessions[session_id]
        
        # Señal para parar orquestación
        if session_id in active_protect_loops:
            active_protect_loops[session_id]["running"] = False
            
        for slave_id in session.slave_ids:
            if slave_id in connected_slaves:
                await manager.send_to_slave(slave_id, {
                    "type": "control",
                    "action": "stop"
                })
        
        # Actualizar estado en DB
        db = SessionLocal()
        try:
            s = db.query(SessionModel).filter(SessionModel.id == session_id).first()
            if s:
                s.status = 'stopped'
                s.updated_at = datetime.utcnow()
                db.commit()
        except SQLAlchemyError as e:
            logger.error(f"DB update session stopped error: {e}")
            db.rollback()
        finally:
            db.close()
            
        return {"status": "stopped", "session_id": session_id}
    
    @app.post("/api/sessions/{session_id}/one-batch")
    async def one_batch(session_id: str):
        """Ejecutar una sola ronda de reparación cooperativa."""
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session = active_sessions[session_id]
        project = active_projects.get(session.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        # Slaves válidos
        valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
        if not valid_slaves:
            raise HTTPException(status_code=400, detail="No valid slaves in session")
        
        # Preparar slaves con modo y proyecto
        for slave_id in valid_slaves:
            await manager.send_to_slave(slave_id, {"type": "setMode", "mode": project.mode})
            await manager.send_to_slave(slave_id, {"type": "loadProject", "config": project.config})
        
        # Forzar preview fresco del favorito
        fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
        if fav_id:
            old_ts = _last_preview_timestamp.get(fav_id, 0)
            await manager.send_to_slave(fav_id, {"type": "guardControl", "action": "check"})
            for _ in range(20):
                await asyncio.sleep(0.25)
                if _last_preview_timestamp.get(fav_id, 0) > old_ts:
                    break
        
        fav = connected_slaves.get(fav_id) if fav_id else None
        preview = (fav.telemetry.get('preview_data') if fav and isinstance(fav.telemetry, dict) else None) or {}
        
        # Filtrar cambios
        def _expected_color(c):
            return c.get('expectedColor', c.get('color', 0))
        
        changes = preview.get('changes', []) if isinstance(preview, dict) else []
        
        # Evitar píxeles bloqueados
        try:
            changes = [c for c in changes if not is_locked_change(c)]
        except Exception:
            pass
            
        # Missing + Absent + Incorrect
        changes = [c for c in changes if c.get('type') in ('missing', 'absent', 'incorrect')]
        
        # Filtros de color
        excluded_ids = set(guard_config.get('excludedColorIds') or []) if guard_config.get('excludeColor') else set()
        preferred_ids = set(guard_config.get('preferredColorIds') or []) if guard_config.get('preferColor') else set()
        changes = [c for c in changes if _expected_color(c) not in excluded_ids]
        
        # Prioridad
        def _prio(c):
            col = _expected_color(c)
            is_missing_or_incorrect = 0 if c.get('type') in ('missing', 'incorrect') else 1
            is_pref = 0 if col in preferred_ids else 1
            return (is_missing_or_incorrect, is_pref)
            
        changes.sort(key=_prio)
        
        # Cargas por bot
        charges: Dict[str, int] = {}
        total_remaining = 0
        for sid in valid_slaves:
            rem = 0
            try:
                rem = int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
            except Exception:
                rem = 0
            charges[sid] = rem
            total_remaining += rem
        
        if not changes:
            return {"ok": True, "session_id": session_id, "assigned": 0, "reason": "no_changes", "total_remaining": total_remaining}
        if total_remaining <= 0:
            return {"ok": True, "session_id": session_id, "assigned": 0, "reason": "no_charges", "total_remaining": total_remaining}
        
        # Planificación una sola ronda
        pixels_per_batch = int(guard_config.get('pixelsPerBatch') or 10)
        spend_all = bool(guard_config.get('spendAllPixelsOnStart'))
        round_total = sum(charges.values()) if spend_all else min(sum(charges.values()), pixels_per_batch)
        if round_total <= 0:
            return {"ok": True, "session_id": session_id, "assigned": 0, "reason": "zero_round_total", "total_remaining": total_remaining}
        
        plan: Dict[str, int] = {sid: 0 for sid in valid_slaves}
        order = [sid for sid in valid_slaves if charges.get(sid, 0) > 0]
        idx = 0
        assigned = 0
        
        while assigned < round_total and order:
            sid = order[idx % len(order)]
            if plan[sid] < charges[sid]:
                plan[sid] += 1
                assigned += 1
            idx += 1
            if all(plan[s] >= charges[s] for s in order) and assigned < round_total:
                break
        
        pick = min(len(changes), sum(plan.values()))
        if pick <= 0:
            return {"ok": True, "session_id": session_id, "assigned": 0, "reason": "no_pick", "total_remaining": total_remaining}
            
        # Aplicar patrón de protección
        try:
            selected = select_pixels_by_pattern(
                str(guard_config.get('protectionPattern', 'random')), 
                changes, 
                pick
            )
        except Exception:
            selected = changes[:pick]
        
        # Agrupar, sublotear y enviar
        TILE = 1000
        queues: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in valid_slaves}
        rr_list = []
        
        for sid in [s for s in valid_slaves if plan.get(s, 0) > 0]:
            rr_list += [sid] * plan[sid]
            
        for i, ch in enumerate(selected):
            sid = rr_list[i]
            queues[sid].append(ch)
        
        req_id = uuid.uuid4().hex
        batch_tracker.create(req_id)
        
        async def _send_sub(slave_id: str, items: List[dict]):
            subtile: Dict[tuple, List[dict]] = defaultdict(list)
            for ch in items:
                x = int(ch.get('x'))
                y = int(ch.get('y'))
                subtile[(x // TILE, y // TILE)].append(ch)
                
            SUB = 40
            for (tx, ty), lst in subtile.items():
                for i in range(0, len(lst), SUB):
                    part = lst[i:i+SUB]
                    coords = [{'x': it['x'], 'y': it['y']} for it in part]
                    colors = [int(it.get('expectedColor', it.get('color', 0))) for it in part]
                    payload = {
                        'tileX': tx, 
                        'tileY': ty, 
                        'coords': coords, 
                        'colors': colors, 
                        'requestId': req_id
                    }
                    batch_tracker.assign(req_id, slave_id, payload, 0)
                    await manager.send_to_slave(slave_id, {'type': 'paintBatch', **payload})
        
        for sid, items in queues.items():
            if items:
                await _send_sub(sid, items)
        
        # Esperar resultados con reintentos/reasignación
        deadline = asyncio.get_event_loop().time() + 45.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.3)
            if batch_tracker.get_pending(req_id) == 0:
                break
                
            fails = batch_tracker.failed_assignments(req_id)
            for (sid, key), data in fails:
                candidates = (
                    [x for x in valid_slaves if x != sid and charges.get(x, 0) > 0] or
                    [x for x in valid_slaves if x != sid] or 
                    valid_slaves
                )
                idx = (idx + 1) if isinstance(idx, int) else 0
                new_sid = candidates[idx % len(candidates)]
                attempts = batch_tracker.inc_attempts(req_id, sid, key)
                
                if attempts <= 3:
                    await manager.send_to_slave(new_sid, {
                        'type': 'paintBatch',
                        'tileX': data['tileX'],
                        'tileY': data['tileY'],
                        'coords': data['coords'],
                        'colors': data['colors'],
                        'requestId': req_id
                    })
        
        return {
            "ok": True,
            "session_id": session_id,
            "assigned": pick,
            "total_remaining": total_remaining,
            "plan": plan
        }