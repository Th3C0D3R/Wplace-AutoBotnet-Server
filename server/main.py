from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import asyncio
import uuid
from datetime import datetime
import logging
from threading import Lock
from collections import defaultdict
from fastapi import UploadFile, File
from sqlalchemy import create_engine, Column, String, DateTime, JSON, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError

# Nota: Endpoint especial para cargar config Guard y reenviarla al slave favorito

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WPlace Master Server", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class SlaveInfo(BaseModel):
    id: str
    connected_at: datetime
    last_seen: datetime
    status: str = "idle"  # idle, working, error
    mode: Optional[str] = None  # Image, Guard, Farm
    telemetry: Dict[str, Any] = {}
    is_favorite: bool = False  # NUEVO: marca de Fav-Slave

class PixelBatch(BaseModel):
    tileX: int
    tileY: int
    coords: List[Dict[str, int]]  # [{x, y}, ...]
    colors: List[int]

class ProjectConfig(BaseModel):
    name: str
    mode: str  # Image, Guard
    config: Dict[str, Any]
    chunks: List[Dict[str, Any]] = []

class SessionConfig(BaseModel):
    project_id: str
    slave_ids: List[str]
    strategy: str = "balanced"  # balanced, drain, priority

class GuardUpload(BaseModel):
    filename: Optional[str] = None
    data: Dict[str, Any]
    # data contendrá estructura como la generada por save-load.js (protectionData, originalPixels, colors, etc.)

# In-memory storage (replace with Redis/Postgres later)
connected_slaves: Dict[str, SlaveInfo] = {}
active_projects: Dict[str, ProjectConfig] = {}
active_sessions: Dict[str, SessionConfig] = {}
guard_config: Dict[str, Any] = {
    # Valores básicos iniciales (coinciden con defaults de guardState relevantes)
    "protectionPattern": "random",
    "preferColor": False,
    "preferredColorIds": [],
    "excludeColor": False,
    "excludedColorIds": [],
    "spendAllPixelsOnStart": False,
    "minChargesToWait": 20,
    "pixelsPerBatch": 10,
    "randomWaitTime": False,
    "randomWaitMin": 5,
    "randomWaitMax": 15,
    "watchMode": True,  # por defecto sólo observar
    "colorThreshold": 10,
    "autoDistribute": False,  # nuevo: permitir distribución automática desde UI futura
    "colorComparisonMethod": "rgb"  # nuevo: 'rgb' o 'lab'
}
websocket_connections: Dict[str, WebSocket] = {}
ui_connections: List[WebSocket] = []
active_protect_loops: Dict[str, Dict[str, Any]] = {}
# Último guardData subido, para reenviarlo a un nuevo favorito si cambia
last_guard_upload: Optional[Dict[str, Any]] = None
# Selección de slaves a nivel UI (persistente en memoria; usado como default cross-device)
ui_selected_slaves: List[str] = []

# === DB (SQLite) setup ===
DATABASE_URL = "sqlite:///./master.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ProjectModel(Base):
    __tablename__ = "projects"
    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    config = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class SessionModel(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True, index=True)
    project_id = Column(String, nullable=False)
    slave_ids = Column(JSON, nullable=False)
    strategy = Column(String, default="balanced")
    status = Column(String, default="created")  # created | running | paused | stopped
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("DB initialized (SQLite)")
    except SQLAlchemyError as e:
        logger.error(f"DB init error: {e}")

@app.on_event("startup")
async def on_startup():
    # Initialize DB and load persisted projects/sessions to memory maps
    init_db()
    db = SessionLocal()
    try:
        # Load projects
        for p in db.query(ProjectModel).all():
            active_projects[p.id] = ProjectConfig(name=p.name, mode=p.mode, config=p.config, chunks=[])
        # Load sessions (keep created, running, paused)
        persisted = db.query(SessionModel).all()
        for s in persisted:
            active_sessions[s.id] = SessionConfig(project_id=s.project_id, slave_ids=list(s.slave_ids or []), strategy=s.strategy)
            # No auto-resume loops on server restart; UI can resume explicitly
        logger.info(f"Loaded {len(active_projects)} projects and {len(active_sessions)} sessions from DB")
    except SQLAlchemyError as e:
        logger.error(f"Startup DB load error: {e}")
    finally:
        db.close()

# Seguimiento de lotes y reintentos
class BatchTracker:
    def __init__(self):
        # requestId -> { 'assignments': { (slave_id, batch_key): {tileX,tileY,coords,colors,attempts,status,last_assigned_to} }, 'pending': int }
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.lock = Lock()

    def create(self, request_id: str):
        with self.lock:
            self.batches[request_id] = {'assignments': {}, 'pending': 0}

    def _key(self, slave_id: str, payload: Dict[str, Any]):
        # key único por tile y primer coord
        coords = payload.get('coords') or []
        if coords:
            c0 = coords[0]
            return f"{payload.get('tileX')},{payload.get('tileY')}:{c0.get('x')},{c0.get('y')}"
        return f"{payload.get('tileX')},{payload.get('tileY')}:empty"

    def assign(self, request_id: str, slave_id: str, payload: Dict[str, Any], attempt: int):
        with self.lock:
            if request_id not in self.batches:
                self.batches[request_id] = {'assignments': {}, 'pending': 0}
            key = self._key(slave_id, payload)
            self.batches[request_id]['assignments'][(slave_id, key)] = {
                **payload,
                'attempts': attempt,
                'status': 'pending',
                'last_assigned_to': slave_id
            }
            self._recount(request_id)

    def mark(self, request_id: str, slave_id: str, tileX: int, tileY: int, coords: List[Dict[str, int]], ok: bool):
        with self.lock:
            b = self.batches.get(request_id)
            if not b:
                return
            tmp_payload = {'tileX': tileX, 'tileY': tileY, 'coords': coords}
            key = self._key(slave_id, tmp_payload)
            k = (slave_id, key)
            if k in b['assignments']:
                b['assignments'][k]['status'] = 'ok' if ok else 'failed'
            self._recount(request_id)

    def failed_assignments(self, request_id: str):
        with self.lock:
            b = self.batches.get(request_id, {})
            return [((sid, key), data) for (sid, key), data in b.get('assignments', {}).items() if data.get('status') == 'failed']

    def inc_attempts(self, request_id: str, sid: str, key: str) -> int:
        with self.lock:
            b = self.batches.get(request_id)
            if not b: return 0
            if (sid, key) not in b['assignments']: return 0
            b['assignments'][(sid, key)]['attempts'] = int(b['assignments'][(sid, key)].get('attempts', 0)) + 1
            b['assignments'][(sid, key)]['status'] = 'pending'
            return b['assignments'][(sid, key)]['attempts']

    def get_pending(self, request_id: str) -> int:
        with self.lock:
            b = self.batches.get(request_id, {})
            return int(b.get('pending', 0))

    def _recount(self, request_id: str):
        b = self.batches.get(request_id, {})
        b['pending'] = sum(1 for _k, a in b.get('assignments', {}).items() if a.get('status') == 'pending')

batch_tracker = BatchTracker()

# Control simple para esperas de preview tras un check manual
_last_preview_lock = Lock()
_last_preview_timestamp: Dict[str, float] = {}

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.slave_connections: Dict[str, WebSocket] = {}
        self.ui_connections: List[WebSocket] = []

    async def connect_slave(self, websocket: WebSocket, slave_id: str):
        await websocket.accept()
        # Si ya existe una conexión con el mismo ID, cerrarla y reemplazarla
        if slave_id in self.slave_connections:
            try:
                await self.slave_connections[slave_id].close()
            except Exception:
                pass
        self.slave_connections[slave_id] = websocket
        if slave_id not in connected_slaves:
            # Verificar si es el primer slave conectado para marcarlo como favorito
            is_first_slave = len(connected_slaves) == 0
            
            connected_slaves[slave_id] = SlaveInfo(
                id=slave_id,
                connected_at=datetime.now(),
                last_seen=datetime.now(),
                is_favorite=is_first_slave
            )
            
            await self.broadcast_to_ui({"type": "slave_connected", "slave_id": slave_id})
            
            if is_first_slave:
                await self.broadcast_to_ui({"type": "favorite_set", "slave_id": slave_id})
                logger.info(f"Slave {slave_id} connected and set as favorite (first slave)")
                # Enviar configuración guard actual al favorito
                try:
                    await self.slave_connections[slave_id].send_text(json.dumps({
                        "type": "guardConfig",
                        "config": guard_config,
                        "timestamp": datetime.utcnow().isoformat()
                    }))
                except Exception as _e:
                    logger.error(f"Error sending guard config to first favorite {slave_id}: {_e}")
                # Enviar guardData si existe para continuar preview
                try:
                    if last_guard_upload:
                        await self.slave_connections[slave_id].send_text(json.dumps({
                            "type": "guardData",
                            "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                            "guardData": last_guard_upload.get("data", {}),
                            "timestamp": datetime.utcnow().isoformat()
                        }))
                except Exception as _e:
                    logger.error(f"Error sending guardData to first favorite {slave_id}: {_e}")
            else:
                logger.info(f"Slave {slave_id} connected")
        else:
            # Re-conexión: actualizar last_seen y notificar opcionalmente
            connected_slaves[slave_id].last_seen = datetime.now()
            await self.broadcast_to_ui({"type": "slave_reconnected", "slave_id": slave_id})
            logger.info(f"Slave {slave_id} reconnected")
            # Si es favorito al reconectar, reenviar config guard
            if connected_slaves[slave_id].is_favorite:
                try:
                    await self.slave_connections[slave_id].send_text(json.dumps({
                        "type": "guardConfig",
                        "config": guard_config,
                        "timestamp": datetime.utcnow().isoformat()
                    }))
                except Exception as _e:
                    logger.error(f"Error re-sending guard config to favorite {slave_id}: {_e}")
                # También re-enviar guardData si existe
                try:
                    if last_guard_upload:
                        await self.slave_connections[slave_id].send_text(json.dumps({
                            "type": "guardData",
                            "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                            "guardData": last_guard_upload.get("data", {}),
                            "timestamp": datetime.utcnow().isoformat()
                        }))
                except Exception as _e:
                    logger.error(f"Error re-sending guardData to favorite {slave_id}: {_e}")

    async def disconnect_slave(self, slave_id: str):
        if slave_id in self.slave_connections:
            del self.slave_connections[slave_id]
        # Detectar si era favorito y eliminar
        was_favorite = False
        if slave_id in connected_slaves:
            try:
                was_favorite = bool(getattr(connected_slaves[slave_id], 'is_favorite', False))
            except Exception:
                was_favorite = False
            del connected_slaves[slave_id]
        await self.broadcast_to_ui({"type": "slave_disconnected", "slave_id": slave_id})
        logger.info(f"Slave {slave_id} disconnected")
        # Si el favorito se desconectó, elegir otro aleatoriamente y notificar
        if was_favorite and connected_slaves:
            try:
                new_id = next(iter(connected_slaves.keys()))
                # Desmarcar todos y marcar nuevo favorito
                for sid, s in connected_slaves.items():
                    s.is_favorite = (sid == new_id)
                # Avisar al nuevo favorito
                await self.send_to_slave(new_id, {"type": "setFavorite", "isFavorite": True})
                # Enviar configuración Guard para que siga telemetría coherente
                await self.send_to_slave(new_id, {"type": "guardConfig", "config": guard_config, "timestamp": datetime.utcnow().isoformat()})
                # Enviar guardData si existe para que continúe la preview
                if last_guard_upload:
                    await self.send_to_slave(new_id, {
                        "type": "guardData",
                        "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                        "guardData": last_guard_upload.get("data", {}),
                        "timestamp": datetime.utcnow().isoformat()
                    })
                # Notificar a UIs
                await self.broadcast_to_ui({"type": "slave_favorite", "slave_id": new_id})
                logger.info(f"Reassigned favorite to {new_id} after {slave_id} disconnect")
            except Exception as e:
                logger.error(f"Failed to auto-assign new favorite after disconnect: {e}")

    async def connect_ui(self, websocket: WebSocket):
        await websocket.accept()
        self.ui_connections.append(websocket)
        logger.info("UI client connected")

    async def disconnect_ui(self, websocket: WebSocket):
        if websocket in self.ui_connections:
            self.ui_connections.remove(websocket)
        logger.info("UI client disconnected")

    async def send_to_slave(self, slave_id: str, message: dict):
        if slave_id in self.slave_connections:
            try:
                await self.slave_connections[slave_id].send_text(json.dumps(message))
            except Exception as e:
                logger.error(f"Error sending to slave {slave_id}: {e}")
                await self.disconnect_slave(slave_id)

    async def broadcast_to_ui(self, message: dict):
        disconnected = []
        for connection in self.ui_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception as e:
                logger.error(f"Error broadcasting to UI: {e}")
                disconnected.append(connection)
        
        for connection in disconnected:
            await self.disconnect_ui(connection)

manager = ConnectionManager()

# REST API Endpoints
@app.get("/api/slaves")
async def get_slaves():
    """Get list of connected slaves"""
    return {"slaves": list(connected_slaves.values())}

@app.get("/api/guard/config")
async def get_guard_config():
    """Obtener configuración global Guard almacenada en servidor."""
    return {"config": guard_config}

# === UI selection persistence (cross-browser defaults) ===
class SelectedSlavesUpdate(BaseModel):
    slave_ids: List[str]

@app.get("/api/ui/selected-slaves")
async def get_ui_selected_slaves():
    return {"slave_ids": list(ui_selected_slaves)}

@app.post("/api/ui/selected-slaves")
async def set_ui_selected_slaves(sel: SelectedSlavesUpdate):
    global ui_selected_slaves
    # Guardar sólo ids que existan actualmente o cualquier id (permitir persistir futuros)
    try:
        ui_selected_slaves = list(dict.fromkeys(sel.slave_ids))
    except Exception:
        ui_selected_slaves = sel.slave_ids or []
    await manager.broadcast_to_ui({"type": "ui_selected_slaves", "slave_ids": ui_selected_slaves})
    return {"ok": True, "slave_ids": ui_selected_slaves}

class GuardConfigUpdate(BaseModel):
    protectionPattern: Optional[str] = None
    preferColor: Optional[bool] = None
    preferredColorIds: Optional[List[int]] = None
    excludeColor: Optional[bool] = None
    excludedColorIds: Optional[List[int]] = None
    spendAllPixelsOnStart: Optional[bool] = None
    minChargesToWait: Optional[int] = None
    pixelsPerBatch: Optional[int] = None
    randomWaitTime: Optional[bool] = None
    randomWaitMin: Optional[float] = None
    randomWaitMax: Optional[float] = None
    watchMode: Optional[bool] = None
    colorThreshold: Optional[int] = None
    autoDistribute: Optional[bool] = None
    colorComparisonMethod: Optional[str] = None  # 'rgb' | 'lab'

@app.post("/api/guard/config")
async def update_guard_config(cfg: GuardConfigUpdate):
    """Actualizar configuración guard y broadcast al slave favorito."""
    changed = {}
    for field, value in cfg.dict(exclude_unset=True).items():
        guard_config[field] = value
        changed[field] = value

    # Localizar slave favorito
    fav_id = None
    for sid, sinfo in connected_slaves.items():
        if getattr(sinfo, 'is_favorite', False):
            fav_id = sid
            break
    if fav_id:
        await manager.send_to_slave(fav_id, {
            "type": "guardConfig",
            "config": guard_config,
            "changed": changed,
            "timestamp": datetime.utcnow().isoformat()
        })
    # Notificar a UIs
    await manager.broadcast_to_ui({
        "type": "guard_config",
        "config": guard_config,
        "changed": changed
    })
    return {"ok": True, "config": guard_config, "changed": changed}

@app.post("/api/guard/check")
async def guard_force_check():
    """Forzar un análisis inmediato en el slave favorito (preview refresh)."""
    fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    if not fav_id:
        # Fallback: usar primer slave conectado
        if connected_slaves:
            fav_id = next(iter(connected_slaves.keys()))
            logger.warning(f"[GUARD UPLOAD] No favorite slave; usando fallback {fav_id}")
        else:
            raise HTTPException(status_code=400, detail="No favorite slave connected")
    await manager.send_to_slave(fav_id, {"type": "guardControl", "action": "check"})
    return {"ok": True, "requested": fav_id}

@app.post("/api/guard/clear")
async def guard_clear_state():
    """Limpiar el estado de Guard en el slave favorito y en el servidor.
    - Envía una orden de limpieza al Fav-Slave.
    - Borra preview_data y contadores guard en memoria.
    - Notifica a las UIs para resetear su estado.
    """
    fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    target_id = fav_id
    if not target_id:
        if connected_slaves:
            target_id = next(iter(connected_slaves.keys()))
            logger.warning(f"[GUARD CLEAR] No favorite slave; usando fallback {target_id}")
        else:
            target_id = None

    # Orden de limpieza al slave (si existe alguno)
    if target_id:
        await manager.send_to_slave(target_id, {"type": "guardControl", "action": "clear"})

    # Limpiar telemetry almacenada en servidor
    try:
        sinfo = connected_slaves.get(target_id) if target_id else None
        if sinfo and isinstance(sinfo.telemetry, dict):
            sinfo.telemetry.pop('preview_data', None)
            for k in ['correctPixels', 'incorrectPixels', 'missingPixels']:
                sinfo.telemetry.pop(k, None)
    except Exception as e:
        logger.error(f"Error clearing telemetry for {target_id}: {e}")

    # Limpiar último guardData persistido para evitar reenvíos tras 'clear'
    global last_guard_upload
    last_guard_upload = None

    # Notificar a las UIs
    await manager.broadcast_to_ui({
        "type": "guard_cleared",
    "slave_id": target_id,
    "guardDataCleared": True
    })

    resp = {"ok": True, "cleared": target_id}
    if not target_id:
        resp["skipped"] = "no_slave_connected"
    return resp

@app.post("/api/projects/clear-all")
async def clear_all_projects_and_sessions():
    """Eliminar todos los proyectos y sesiones (DB y memoria) y detener orquestación.
    También limpia el último guardData almacenado para evitar rehidratación accidental.
    """
    # Detener bucles activos
    try:
        for sid, loop in list(active_protect_loops.items()):
            try:
                loop["running"] = False
            except Exception:
                pass
        active_protect_loops.clear()
    except Exception as e:
        logger.error(f"Error stopping active loops: {e}")

    # Limpiar memoria de proyectos y sesiones
    active_projects.clear()
    active_sessions.clear()

    # Borrar de la base de datos
    db = SessionLocal()
    proj_deleted = sess_deleted = 0
    try:
        sess_deleted = db.query(SessionModel).delete()
        proj_deleted = db.query(ProjectModel).delete()
        db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB clear-all error: {e}")
        db.rollback()
    finally:
        db.close()

    # Limpiar último guardData
    global last_guard_upload
    last_guard_upload = None

    # Notificar a la UI
    await manager.broadcast_to_ui({
        "type": "projects_cleared",
        "sessions_deleted": sess_deleted,
        "projects_deleted": proj_deleted
    })

    return {"ok": True, "projects_deleted": proj_deleted, "sessions_deleted": sess_deleted}

@app.get("/api/guard/preview")
async def guard_get_preview():
    """Obtener último preview_data persistido del slave favorito."""
    fav = next(((sid, s) for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    if not fav:
        raise HTTPException(status_code=404, detail="No favorite slave connected")
    fav_id, fav_state = fav
    pdata = fav_state.telemetry.get('preview_data') if fav_state.telemetry else None
    if not pdata:
        raise HTTPException(status_code=404, detail="No preview_data yet")
    return {"ok": True, "slave_id": fav_id, "data": pdata}

class GuardRepairRequest(BaseModel):
    limit: Optional[int] = 0  # 0 = usar config
    pattern: Optional[str] = None
    watchMode: Optional[bool] = None

@app.post("/api/guard/repair")
async def guard_force_repair(req: GuardRepairRequest):
    """Solicitar una reparación inmediata (batch) en el slave favorito."""
    fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    if not fav_id:
        raise HTTPException(status_code=400, detail="No favorite slave connected")
    payload = {"type": "guardControl", "action": "repair", "params": req.dict(exclude_unset=True)}
    await manager.send_to_slave(fav_id, payload)
    return {"ok": True, "requested": fav_id, "params": payload.get("params")}

@app.post("/api/guard/stop")
async def guard_stop():
    """Intentar detener cualquier actividad de pintura/guard en curso en el slave favorito.
    Envía control stop a la sesión si aplica y una señal de control general al slave favorito.
    """
    fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    target_id = fav_id
    if not target_id:
        # Fallback: usar primer slave conectado si existe
        if connected_slaves:
            target_id = next(iter(connected_slaves.keys()))
            logger.warning(f"[GUARD STOP] No favorite slave; usando fallback {target_id}")
        else:
            # Nada que parar; responder OK para no romper flujos de UI
            return {"ok": True, "requested": None, "skipped": "no_slave_connected"}
    # Señal general de control stop
    await manager.send_to_slave(target_id, { "type": "control", "action": "stop" })
    # También desactivar watch si estuviera corriendo (best-effort)
    try:
        await manager.send_to_slave(target_id, { "type": "guardControl", "action": "toggleWatch" })
    except Exception:
        pass
    return {"ok": True, "requested": target_id}

@app.post("/api/guard/toggle-watch")
async def guard_toggle_watch():
    fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
    target_id = fav_id
    if not target_id:
        if connected_slaves:
            target_id = next(iter(connected_slaves.keys()))
            logger.warning(f"[GUARD TOGGLE WATCH] No favorite slave; usando fallback {target_id}")
        else:
            return {"ok": True, "requested": None, "skipped": "no_slave_connected"}
    await manager.send_to_slave(target_id, {"type": "guardControl", "action": "toggleWatch"})
    return {"ok": True, "requested": target_id}

@app.post("/api/slaves/{slave_id}/favorite")
async def set_favorite_slave(slave_id: str):
    """Mark a single slave as favorite (only one at a time)."""
    if slave_id not in connected_slaves:
        raise HTTPException(status_code=404, detail="Slave not found")
    
    # Unset previous favorite and notify
    for sid, s in connected_slaves.items():
        if getattr(s, 'is_favorite', False):
            s.is_favorite = False
            await manager.send_to_slave(sid, {
                "type": "setFavorite",
                "isFavorite": False
            })
    
    # Set new favorite and notify
    connected_slaves[slave_id].is_favorite = True
    await manager.send_to_slave(slave_id, {
        "type": "setFavorite",
        "isFavorite": True
    })
    # Push current guard config to new favorite
    try:
        await manager.send_to_slave(slave_id, {
            "type": "guardConfig",
            "config": guard_config,
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as _e:
        logger.error(f"Error sending guard config to new favorite {slave_id}: {_e}")
    # Enviar último guardData si existe para reactivar preview
    try:
        if last_guard_upload:
            await manager.send_to_slave(slave_id, {
                "type": "guardData",
                "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                "guardData": last_guard_upload.get("data", {}),
                "timestamp": datetime.utcnow().isoformat()
            })
    except Exception as _e:
        logger.error(f"Error sending guardData to new favorite {slave_id}: {_e}")
    
    # Notify UI
    await manager.broadcast_to_ui({
        "type": "slave_favorite",
        "slave_id": slave_id
    })
    return {"ok": True, "favorite": slave_id}

@app.get("/api/projects")
async def get_projects():
    """Get list of active projects"""
    return {"projects": list(active_projects.values())}

@app.post("/api/projects")
async def create_project(project: ProjectConfig):
    """Create a new project"""
    project_id = str(uuid.uuid4())
    active_projects[project_id] = project
    # Persist in DB
    db = SessionLocal()
    try:
        db.add(ProjectModel(id=project_id, name=project.name, mode=project.mode, config=project.config))
        db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB save project error: {e}")
        db.rollback()
    finally:
        db.close()
    return {"project_id": project_id, "project": project}

@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """Get specific project"""
    if project_id not in active_projects:
        raise HTTPException(status_code=404, detail="Project not found")
    return active_projects[project_id]

@app.post("/api/sessions")
async def create_session(session: SessionConfig):
    """Create a new work session"""
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = session
    # Persist in DB
    db = SessionLocal()
    try:
        db.add(SessionModel(id=session_id, project_id=session.project_id, slave_ids=session.slave_ids, strategy=session.strategy, status='created'))
        db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB save session error: {e}")
        db.rollback()
    finally:
        db.close()
    return {"session_id": session_id, "session": session}

@app.post("/api/sessions/{session_id}/start")
async def start_session(session_id: str):
    """Start a work session"""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session = active_sessions[session_id]
    project = active_projects.get(session.project_id)
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # 1) Preparar slaves con modo y proyecto
    valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
    if not valid_slaves:
        raise HTTPException(status_code=400, detail="No valid slaves in session")
    for slave_id in valid_slaves:
        await manager.send_to_slave(slave_id, {"type": "setMode", "mode": project.mode})
        await manager.send_to_slave(slave_id, {"type": "loadProject", "config": project.config})
    # Lanzar bucle continuo en segundo plano
    active_protect_loops[session_id] = {"running": True}
    # Update status in DB
    db = SessionLocal()
    try:
        s = db.query(SessionModel).filter(SessionModel.id==session_id).first()
        if s:
            s.status = 'running'; s.updated_at = datetime.utcnow(); db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB update session running error: {e}")
        db.rollback()
    finally:
        db.close()

    async def filter_changes(preview_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        changes = preview_data.get('changes', []) if isinstance(preview_data, dict) else []
        # Elegibles para reparación: missing, absent e incorrect
        # Nota: El usuario requiere que "Missing Pixel" se trate igual que "incorrect".
        changes = [c for c in changes if c.get('type') in ('missing', 'absent', 'incorrect')]
        # Aplicar filtros de color de guard_config
        excluded_ids = set(guard_config.get('excludedColorIds') or []) if guard_config.get('excludeColor') else set()
        preferred_ids = set(guard_config.get('preferredColorIds') or []) if guard_config.get('preferColor') else set()
        def exp_color(c):
            return c.get('expectedColor', c.get('color', 0))
        changes = [c for c in changes if exp_color(c) not in excluded_ids]
        # prioridad: missing primero y preferidos primero
        def prio(c):
            col = exp_color(c)
            # Tratar incorrect igual que missing en prioridad
            is_missing_or_incorrect = 0 if c.get('type') in ('missing', 'incorrect') else 1
            is_pref = 0 if col in preferred_ids else 1
            return (is_missing_or_incorrect, is_pref)
        changes.sort(key=prio)
        return changes

    async def orchestrate_loop():
        try:
            while active_protect_loops.get(session_id, {}).get('running'):
                # Recopilar slaves válidos dinámicamente (sesión ∩ conectados)
                current_valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
                if not current_valid_slaves:
                    await asyncio.sleep(3)
                    continue
                # 2) Preview del favorito
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

                # Charges por bot (usar sólo los conectados actuales)
                charges: Dict[str, int] = {}
                total_remaining = 0
                for sid in current_valid_slaves:
                    rem = 0
                    try:
                        rem = int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
                    except Exception:
                        rem = 0
                    charges[sid] = rem
                    total_remaining += rem

                # Si no hay cambios, esperar indefinidamente hasta que aparezcan (modo standby)
                if not changes:
                    await asyncio.sleep(5)
                    continue
                if total_remaining <= 0:
                    # Esperar regeneración (30s)
                    await asyncio.sleep(30)
                    continue

                # 3) Planificación del lote por ronda
                pixels_per_batch = int(guard_config.get('pixelsPerBatch') or 10)
                spend_all = bool(guard_config.get('spendAllPixelsOnStart'))
                # Objetivo total de esta ronda
                round_total = sum(charges.values()) if spend_all else min(sum(charges.values()), pixels_per_batch)
                if round_total <= 0:
                    await asyncio.sleep(5)
                    continue

                # Asignación por bot: no superar sus cargas
                plan: Dict[str, int] = { sid: 0 for sid in current_valid_slaves }
                # greedy round-robin
                order = [sid for sid in current_valid_slaves if charges.get(sid, 0) > 0]
                idx = 0
                assigned = 0
                while assigned < round_total and order:
                    sid = order[idx % len(order)]
                    if plan[sid] < charges[sid]:
                        plan[sid] += 1
                        assigned += 1
                    idx += 1
                    # salir si ya todos alcanzaron su tope de cargas
                    if all(plan[s] >= charges[s] for s in order) and assigned < round_total:
                        break

                # Seleccionar los primeros N cambios a pintar (en orden)
                pick = min(len(changes), sum(plan.values()))
                if pick <= 0:
                    await asyncio.sleep(5)
                    continue
                selected = changes[:pick]

                # Agrupar por tile y repartir por bot según su cupo (manteniendo orden)
                TILE = 1000
                by_tile = defaultdict(list)
                for ch in selected:
                    x = int(ch.get('x')); y = int(ch.get('y'))
                    tx = x // TILE; ty = y // TILE
                    by_tile[(tx, ty)].append(ch)

                # Construir colas por slave: asignamos round-robin por plan
                queues: Dict[str, List[Dict[str, Any]]] = { sid: [] for sid in current_valid_slaves }
                sid_order = [sid for sid in current_valid_slaves if plan.get(sid, 0) > 0]
                cursor_by_sid = { sid: 0 for sid in sid_order }
                # lista lineal de selected para asignación RR respetando plan
                rr_list = []
                for sid in sid_order:
                    rr_list += [sid] * plan[sid]
                # rr_list es del tamaño de pick; asignamos en ese orden
                for i, ch in enumerate(selected):
                    sid = rr_list[i]
                    queues[sid].append(ch)

                # Envío por sublotes respetando tile y BatchTracker
                req_id = uuid.uuid4().hex
                batch_tracker.create(req_id)

                async def send_sub(slave_id: str, items: List[dict]):
                    # reagrupa por tile y trocea a SUB
                    subtile: Dict[tuple, List[dict]] = defaultdict(list)
                    for ch in items:
                        x = int(ch.get('x')); y = int(ch.get('y'))
                        subtile[(x // TILE, y // TILE)].append(ch)
                    SUB = 40
                    for (tx, ty), lst in subtile.items():
                        for i in range(0, len(lst), SUB):
                            part = lst[i:i+SUB]
                            coords = [{ 'x': it['x'], 'y': it['y'] } for it in part]
                            colors = [int(it.get('expectedColor', it.get('color', 0))) for it in part]
                            payload = { 'tileX': tx, 'tileY': ty, 'coords': coords, 'colors': colors, 'requestId': req_id }
                            batch_tracker.assign(req_id, slave_id, payload, 0)
                            await manager.send_to_slave(slave_id, { 'type': 'paintBatch', **payload })

                # enviar colas
                for sid, items in queues.items():
                    if items:
                        await send_sub(sid, items)

                # Esperar resultados y reintentar/reasignar
                deadline = asyncio.get_event_loop().time() + 90.0
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.3)
                    if batch_tracker.get_pending(req_id) == 0:
                        break
                    fails = batch_tracker.failed_assignments(req_id)
                    for (sid, key), data in fails:
                        # elegir otro slave con cargas restantes en ese momento
                        candidates = [x for x in valid_slaves if x != sid and charges.get(x, 0) > 0] or [x for x in valid_slaves if x != sid]
                        if not candidates:
                            candidates = valid_slaves
                        new_sid = candidates[(idx := (idx + 1)) % len(candidates)]
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
                # Pequeño respiro antes de siguiente ronda
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"orchestrate_loop error: {e}")

    # Ejecutar en background
    asyncio.create_task(orchestrate_loop())

    # Responder de inmediato con sumatorio de charges actuales
    total_remaining = 0
    for sid in valid_slaves:
        try:
            total_remaining += int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
        except Exception:
            pass
    return {"status": "started", "session_id": session_id, "total_remaining": total_remaining}

@app.post("/api/sessions/{session_id}/pause")
async def pause_session(session_id: str):
    """Pause a work session"""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session = active_sessions[session_id]
    
    for slave_id in session.slave_ids:
        if slave_id in connected_slaves:
            await manager.send_to_slave(slave_id, {
                "type": "control",
                "action": "pause"
            })
    
    # Update status in DB
    db = SessionLocal()
    try:
        s = db.query(SessionModel).filter(SessionModel.id==session_id).first()
        if s:
            s.status = 'paused'; s.updated_at = datetime.utcnow(); db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB update session paused error: {e}")
        db.rollback()
    finally:
        db.close()
    return {"status": "paused", "session_id": session_id}

@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    """Stop a work session"""
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
    
    # Update status in DB
    db = SessionLocal()
    try:
        s = db.query(SessionModel).filter(SessionModel.id==session_id).first()
        if s:
            s.status = 'stopped'; s.updated_at = datetime.utcnow(); db.commit()
    except SQLAlchemyError as e:
        logger.error(f"DB update session stopped error: {e}")
        db.rollback()
    finally:
        db.close()
    return {"status": "stopped", "session_id": session_id}

@app.post("/api/sessions/{session_id}/one-batch")
async def one_batch(session_id: str):
    """Run a single cooperative repair round using the same planner as the continuous loop.
    - Refreshes preview from favorite
    - Filters Missing + Absent and applies Guard color filters
    - Computes per-bot quotas capped by remaining charges and pixelsPerBatch
    - Dispatches sublots with retries/reassignment (<=3 attempts)
    - Returns assigned count and quick summary
    """
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

    # 1) Preparar slaves con modo y proyecto (por si no se inició el bucle continuo)
    valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
    if not valid_slaves:
        raise HTTPException(status_code=400, detail="No valid slaves in session")
    for slave_id in valid_slaves:
        await manager.send_to_slave(slave_id, {"type": "setMode", "mode": project.mode})
        await manager.send_to_slave(slave_id, {"type": "loadProject", "config": project.config})

    # 2) Forzar preview fresco del favorito (si existe)
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

    # 3) Filtrar cambios (Missing + Absent + Incorrect) y aplicar filtros de color
    def _expected_color(c):
        return c.get('expectedColor', c.get('color', 0))

    changes = preview.get('changes', []) if isinstance(preview, dict) else []
    # Missing + Absent + Incorrect
    changes = [c for c in changes if c.get('type') in ('missing', 'absent', 'incorrect')]
    # filtros de color
    excluded_ids = set(guard_config.get('excludedColorIds') or []) if guard_config.get('excludeColor') else set()
    preferred_ids = set(guard_config.get('preferredColorIds') or []) if guard_config.get('preferColor') else set()
    changes = [c for c in changes if _expected_color(c) not in excluded_ids]
    # prioridad: missing primero y preferidos primero
    def _prio(c):
        col = _expected_color(c)
        # Tratar incorrect igual que missing en prioridad
        is_missing_or_incorrect = 0 if c.get('type') in ('missing', 'incorrect') else 1
        is_pref = 0 if col in preferred_ids else 1
        return (is_missing_or_incorrect, is_pref)
    changes.sort(key=_prio)

    # 4) Cargas por bot
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

    # 5) Planificación una sola ronda
    pixels_per_batch = int(guard_config.get('pixelsPerBatch') or 10)
    spend_all = bool(guard_config.get('spendAllPixelsOnStart'))
    round_total = sum(charges.values()) if spend_all else min(sum(charges.values()), pixels_per_batch)
    if round_total <= 0:
        return {"ok": True, "session_id": session_id, "assigned": 0, "reason": "zero_round_total", "total_remaining": total_remaining}

    plan: Dict[str, int] = { sid: 0 for sid in valid_slaves }
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
    selected = changes[:pick]

    # 6) Agrupar, sublotear y enviar
    TILE = 1000
    # Construir colas por slave segun plan
    queues: Dict[str, List[Dict[str, Any]]] = { sid: [] for sid in valid_slaves }
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
            x = int(ch.get('x')); y = int(ch.get('y'))
            subtile[(x // TILE, y // TILE)].append(ch)
        SUB = 40
        for (tx, ty), lst in subtile.items():
            for i in range(0, len(lst), SUB):
                part = lst[i:i+SUB]
                coords = [{ 'x': it['x'], 'y': it['y'] } for it in part]
                colors = [int(it.get('expectedColor', it.get('color', 0))) for it in part]
                payload = { 'tileX': tx, 'tileY': ty, 'coords': coords, 'colors': colors, 'requestId': req_id }
                batch_tracker.assign(req_id, slave_id, payload, 0)
                await manager.send_to_slave(slave_id, { 'type': 'paintBatch', **payload })

    for sid, items in queues.items():
        if items:
            await _send_sub(sid, items)

    # 7) Esperar resultados con reintentos/reasignación
    deadline = asyncio.get_event_loop().time() + 45.0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.3)
        if batch_tracker.get_pending(req_id) == 0:
            break
        fails = batch_tracker.failed_assignments(req_id)
        for (sid, key), data in fails:
            candidates = [x for x in valid_slaves if x != sid and charges.get(x, 0) > 0] or [x for x in valid_slaves if x != sid] or valid_slaves
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

# WebSocket endpoints
@app.websocket("/ws/slave")
async def websocket_slave_endpoint(websocket: WebSocket):
    """WebSocket endpoint for slave connections"""
    # Usar el ID proporcionado por el cliente si existe, si no generar uno aleatorio
    requested_id = websocket.query_params.get('id') if hasattr(websocket, 'query_params') else None
    slave_id = requested_id or f"SLV_{uuid.uuid4().hex[:8].upper()}"
    await manager.connect_slave(websocket, slave_id)
    
    try:
        # Send connection confirmation
        await websocket.send_text(json.dumps({
            "type": "connected",
            "slave_id": slave_id
        }))
        
        # Notificar al slave si es favorito
        if connected_slaves[slave_id].is_favorite:
            await websocket.send_text(json.dumps({
                "type": "favorite_status",
                "is_favorite": True
            }))
        
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            # Update slave info
            if slave_id in connected_slaves:
                connected_slaves[slave_id].last_seen = datetime.now()
                
                if message.get("type") == "telemetry":
                    telem = message.get("data", {})
                    # Normalizar alias previewData -> preview_data
                    if 'previewData' in telem and 'preview_data' not in telem:
                        telem['preview_data'] = telem['previewData']

                    # Fusionar con la telemetría existente para no degradar preview_data rico
                    existing = connected_slaves[slave_id].telemetry if isinstance(connected_slaves[slave_id].telemetry, dict) else {}

                    def _changes_are_detailed(changes):
                        try:
                            return isinstance(changes, list) and len(changes) > 0 and isinstance(changes[0], dict) and ('x' in changes[0])
                        except Exception:
                            return False

                    # Decidir si reemplazar preview_data
                    if 'preview_data' in telem:
                        new_pd = telem.get('preview_data') or {}
                        old_pd = existing.get('preview_data') or {}
                        new_changes = new_pd.get('changes', []) if isinstance(new_pd, dict) else []
                        old_changes = old_pd.get('changes', []) if isinstance(old_pd, dict) else []
                        new_good = _changes_are_detailed(new_changes)
                        old_good = _changes_are_detailed(old_changes)
                        # Reemplazar sólo si el nuevo es detallado o si no teníamos uno bueno
                        if new_good or (not old_good):
                            existing['preview_data'] = new_pd
                        # Eliminar de telem para evitar sobreescritura completa más abajo
                        telem = {k: v for k, v in telem.items() if k != 'preview_data'}

                    # Actualizar el resto de campos de telemetría
                    existing.update(telem)
                    connected_slaves[slave_id].telemetry = existing

                    # Broadcast telemetry to UI
                    await manager.broadcast_to_ui({
                        "type": "telemetry_update",
                        "slave_id": slave_id,
                        "telemetry": connected_slaves[slave_id].telemetry
                    })
                
                elif message.get("type") == "status":
                    connected_slaves[slave_id].status = message.get("status", "idle")
                    await manager.broadcast_to_ui({
                        "type": "status_update",
                        "slave_id": slave_id,
                        "status": message.get("status", "idle")
                    })
                
                # NUEVO: datos de preview del Fav-Slave
                elif message.get("type") == "preview_data":
                    if connected_slaves[slave_id].is_favorite:
                        # Persist preview_data inside telemetry so it's available on UI reload
                        preview_payload = message.get("data", {})
                        try:
                            connected_slaves[slave_id].telemetry["preview_data"] = preview_payload
                            with _last_preview_lock:
                                _last_preview_timestamp[slave_id] = datetime.utcnow().timestamp()
                        except Exception as e:
                            logger.error(f"Failed to persist preview_data for {slave_id}: {e}")
                        await manager.broadcast_to_ui({
                            "type": "preview_data",
                            "slave_id": slave_id,
                            "data": preview_payload
                        })
                        # Auto-distribute si configurado
                        if guard_config.get("autoDistribute"):
                            now_ts = datetime.utcnow().timestamp()
                            last_ts = getattr(manager, '_last_auto_distribute', 0)
                            if (now_ts - last_ts) > 15:  # throttle 15s
                                setattr(manager, '_last_auto_distribute', now_ts)
                                try:
                                    changes = preview_payload.get('changes', [])
                                    if changes:
                                        # Filtrar a estructura pixels para distribución
                                        pixels = []
                                        for ch in changes[:2000]:  # limitar por seguridad
                                            # Tratar 'incorrect' como alta prioridad igual que 'missing'
                                            t = ch.get('type')
                                            high = (t in ('missing', 'incorrect'))
                                            pixels.append({ 'x': ch.get('x'), 'y': ch.get('y'), 'color': ch.get('expectedColor', 0), 'priority': 'high' if high else 'medium' })
                                        # Reutilizar lógica de create_repair_orders (manual) distribuyendo internamente
                                        available_slaves = [ (sid, s) for sid, s in connected_slaves.items() if s.status in ['idle', 'working'] ]
                                        if available_slaves:
                                            # Distribuir simple round-robin
                                            idx = 0
                                            for p in pixels:
                                                sid, _sinfo = available_slaves[idx % len(available_slaves)]
                                                await manager.send_to_slave(sid, {
                                                    'type': 'repairOrder',
                                                    'coords': [ {'x': p['x'], 'y': p['y']} ],
                                                    'colors': [ p['color'] ],
                                                    'source': 'auto_distribute',
                                                    'total_repairs': 1
                                                })
                                                idx += 1
                                            await manager.broadcast_to_ui({
                                                'type': 'auto_distribute_result',
                                                'distributed': len(pixels),
                                                'slaves_used': len(available_slaves)
                                            })
                                        else:
                                            await manager.broadcast_to_ui({ 'type': 'auto_distribute_skipped', 'reason': 'no_slaves' })
                                except Exception as _e:
                                    logger.error(f"Auto distribute error: {_e}")
                elif message.get("type") == "repair_suggestion":
                    # Guard favorite envía sugerencia de reparación
                    await manager.broadcast_to_ui({
                        "type": "repair_suggestion",
                        "slave_id": slave_id,
                        "pixels": message.get("pixels", []),
                        "totalDiffs": message.get("totalDiffs", 0)
                    })
                
                elif message.get("type") == "repair_ack":
                    # Handle repair acknowledgment
                    await manager.broadcast_to_ui({
                        "type": "repair_ack",
                        "slave_id": slave_id,
                        "total_repairs": message.get("total_repairs", 0),
                        "source": message.get("source", "unknown")
                    })
                
                elif message.get("type") == "repair_progress":
                    # Handle repair progress updates
                    await manager.broadcast_to_ui({
                        "type": "repair_progress",
                        "slave_id": slave_id,
                        "completed": message.get("completed", 0),
                        "total": message.get("total", 0),
                        "source": message.get("source", "unknown")
                    })
                
                elif message.get("type") == "repair_complete":
                    # Handle repair completion
                    await manager.broadcast_to_ui({
                        "type": "repair_complete",
                        "slave_id": slave_id,
                        "completed": message.get("completed", 0),
                        "source": message.get("source", "unknown")
                    })
                
                elif message.get("type") == "repair_error":
                    # Handle repair errors
                    await manager.broadcast_to_ui({
                        "type": "repair_error",
                        "slave_id": slave_id,
                        "error": message.get("error", "Unknown error"),
                        "source": message.get("source", "unknown")
                    })
                
                # NUEVO: progreso/resultado de pintura
                elif message.get("type") == "paint_progress":
                    await manager.broadcast_to_ui({
                        "type": "paint_progress",
                        "slave_id": slave_id,
                        "is_favorite": bool(connected_slaves[slave_id].is_favorite),
                        **{k: v for k, v in message.items() if k != 'type'}
                    })
                elif message.get("type") == "paint_result":
                    # Tracking de reintentos
                    req_id = message.get('requestId')
                    try:
                        tX = int(message.get('tileX'))
                        tY = int(message.get('tileY'))
                    except Exception:
                        tX = tY = 0
                    coords = message.get('coords') or []  # opcional
                    if req_id:
                        batch_tracker.mark(req_id, slave_id, tX, tY, coords, bool(message.get('ok')))
                    await manager.broadcast_to_ui({
                        "type": "paint_result",
                        "slave_id": slave_id,
                        "is_favorite": bool(connected_slaves[slave_id].is_favorite),
                        **{k: v for k, v in message.items() if k != 'type'}
                    })
    except WebSocketDisconnect:
        await manager.disconnect_slave(slave_id)
    except Exception as e:
        logger.error(f"Error in slave websocket {slave_id}: {e}")
        await manager.disconnect_slave(slave_id)

@app.websocket("/ws/ui")
async def websocket_ui_endpoint(websocket: WebSocket):
    """WebSocket endpoint for UI real-time updates"""
    await manager.connect_ui(websocket)
    
    try:
        # Send current state
        slaves_data = []
        for slave in connected_slaves.values():
            slave_dict = slave.dict()
            slave_dict['connected_at'] = slave_dict['connected_at'].isoformat()
            slave_dict['last_seen'] = slave_dict['last_seen'].isoformat()
            slaves_data.append(slave_dict)
            
        # Load sessions and projects from DB to include status
        db = SessionLocal()
        try:
            projects_list = []
            for p in db.query(ProjectModel).all():
                projects_list.append({
                    "id": p.id,
                    "name": p.name,
                    "mode": p.mode,
                    "config": p.config
                })
            sessions_list = []
            for s in db.query(SessionModel).all():
                sessions_list.append({
                    "id": s.id,
                    "project_id": s.project_id,
                    "slave_ids": list(s.slave_ids or []),
                    "strategy": s.strategy,
                    "status": s.status,
                })
        finally:
            db.close()
        await websocket.send_text(json.dumps({
            "type": "initial_state",
            "slaves": slaves_data,
            "projects": projects_list,
            "sessions": sessions_list,
            "selected_slaves": list(ui_selected_slaves)
        }))
        
        while True:
            # Keep connection alive
            await websocket.receive_text()
            
    except WebSocketDisconnect:
        await manager.disconnect_ui(websocket)
    except Exception as e:
        logger.error(f"Error in UI websocket: {e}")
        await manager.disconnect_ui(websocket)

# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now()}

@app.post("/api/guard/upload")
async def upload_guard(guard: GuardUpload):
    """Recibe un archivo JSON de guard (estructura de save-load.js) y lo envía al slave favorito.
    El slave favorito debe reconstruir su estado y devolver preview_data rica (como analysis-window).
    """
    # Localizar slave favorito
    fav_id = None
    for sid, sinfo in connected_slaves.items():
        if getattr(sinfo, 'is_favorite', False):
            fav_id = sid
            break
    if not fav_id:
        raise HTTPException(status_code=400, detail="No favorite slave connected")

    # Empaquetar datos relevantes para que el slave pueda reconstruir guardState
    payload = {
        "type": "guardData",
        "filename": guard.filename or "uploaded_guard.json",
        "guardData": guard.data,
        "timestamp": datetime.utcnow().isoformat()
    }
    logger.info(f"[GUARD UPLOAD] Enviando guardData a slave favorito {fav_id} (pixels={len(guard.data.get('originalPixels', []))})")
    # Persistir último guardData para reenvío futuro si cambia el favorito
    global last_guard_upload
    last_guard_upload = {
        "filename": guard.filename or "uploaded_guard.json",
        "data": guard.data,
        "stored_at": datetime.utcnow().isoformat()
    }
    await manager.send_to_slave(fav_id, payload)
    await manager.broadcast_to_ui({
        "type": "guard_upload_sent",
        "slave_id": fav_id,
        "filename": payload["filename"],
        "pixels": len(guard.data.get('originalPixels', []))
    })
    return {"ok": True, "sent_to": fav_id, "filename": payload["filename"]}

@app.post("/api/slaves/{slave_id}/paint")
async def paint_with_slave(slave_id: str, cmd: PixelBatch):
    """Send a paint batch command to a specific slave"""
    if slave_id not in connected_slaves:
        raise HTTPException(status_code=404, detail="Slave not found")
    if len(cmd.coords) != len(cmd.colors) or len(cmd.coords) == 0:
        raise HTTPException(status_code=400, detail="coords/colors length mismatch or empty")
    await manager.send_to_slave(slave_id, {
        "type": "paintBatch",
        "tileX": cmd.tileX,
        "tileY": cmd.tileY,
        "coords": cmd.coords,
        "colors": cmd.colors
    })
    return {"ok": True, "queued": len(cmd.coords)}

class RepairOrder(BaseModel):
    pixels: List[Dict[str, Any]]
    source: str
    timestamp: int

@app.post("/api/repair/orders")
async def create_repair_orders(order: RepairOrder):
    """Create and distribute repair orders to available slaves"""
    if not order.pixels:
        return {"ok": True, "message": "No pixels to repair", "distributed": 0}
    
    # Get all connected slaves (including favorite)
    available_slaves = list(connected_slaves.items())
    
    if not available_slaves:
        raise HTTPException(status_code=400, detail="No available slaves for repair work")
    
    # Sort pixels by priority (high priority first)
    high_priority = [p for p in order.pixels if p.get('priority') == 'high']
    medium_priority = [p for p in order.pixels if p.get('priority') == 'medium']
    low_priority = [p for p in order.pixels if p.get('priority') not in ['high', 'medium']]
    
    sorted_pixels = high_priority + medium_priority + low_priority
    
    # Distribute pixels among available slaves
    pixels_per_slave = len(sorted_pixels) // len(available_slaves)
    remainder = len(sorted_pixels) % len(available_slaves)
    
    distributed_count = 0
    start_idx = 0
    
    for i, (slave_id, slave_info) in enumerate(available_slaves):
        # Calculate how many pixels this slave should handle
        slave_pixels_count = pixels_per_slave + (1 if i < remainder else 0)
        
        if slave_pixels_count == 0:
            continue
            
        # Get the pixels for this slave
        slave_pixels = sorted_pixels[start_idx:start_idx + slave_pixels_count]
        start_idx += slave_pixels_count
        
        # Convert pixels to repair order format
        coords = [{'x': pixel['x'], 'y': pixel['y']} for pixel in slave_pixels]
        colors = [pixel.get('color', 0) for pixel in slave_pixels]
        
        # Send repair order to slave
        await manager.send_to_slave(slave_id, {
            "type": "repairOrder",
            "coords": coords,
            "colors": colors,
            "source": order.source,
            "total_repairs": len(slave_pixels)
        })
        
        distributed_count += len(slave_pixels)
        
        # Log the distribution
        logger.info(f"Sent {len(slave_pixels)} repair orders to slave {slave_id} from {order.source}")
    
    return {
        "ok": True, 
        "message": f"Distributed {distributed_count} repair orders to {len(available_slaves)} slaves",
        "distributed": distributed_count,
        "slaves_used": len(available_slaves)
    }

@app.post("/api/repair/distribute")
async def distribute_repair_orders():
    """Distribute repair orders to all connected slaves based on Fav-Slave analysis"""
    # Find the favorite slave
    fav_slave = None
    for slave_id, slave_info in connected_slaves.items():
        if slave_info.is_favorite:
            fav_slave = (slave_id, slave_info)
            break
    
    if not fav_slave:
        raise HTTPException(status_code=404, detail="No favorite slave found")
    
    fav_slave_id, fav_slave_info = fav_slave
    
    # Get analysis data from favorite slave telemetry
    telemetry = fav_slave_info.telemetry
    if not telemetry or 'preview_data' not in telemetry:
        raise HTTPException(status_code=400, detail="No analysis data available from favorite slave")
    
    preview_data = telemetry['preview_data']

    def _changes_are_detailed(changes):
        try:
            return isinstance(changes, list) and len(changes) > 0 and isinstance(changes[0], dict) and ('x' in changes[0])
        except Exception:
            return False

    changes = preview_data.get('changes', []) if isinstance(preview_data, dict) else []

    # Si los cambios están vacíos o no son detallados, forzar un check y esperar preview nuevo
    if (not changes) or (not _changes_are_detailed(changes)):
        try:
            old_ts = _last_preview_timestamp.get(fav_slave_id, 0)
            await manager.send_to_slave(fav_slave_id, {"type": "guardControl", "action": "check"})
            # Esperar hasta 3s a que llegue un preview_data actualizado
            for _ in range(10):
                await asyncio.sleep(0.3)
                new_ts = _last_preview_timestamp.get(fav_slave_id, 0)
                if new_ts > old_ts:
                    # Recargar preview desde la telemetría persistida
                    telemetry = connected_slaves[fav_slave_id].telemetry
                    preview_data = telemetry.get('preview_data') if isinstance(telemetry, dict) else None
                    changes = preview_data.get('changes', []) if isinstance(preview_data, dict) else []
                    break
        except Exception as _e:
            logger.error(f"Error forcing guard check before distribute: {_e}")

    if (not changes) or (not _changes_are_detailed(changes)):
        return {"ok": True, "message": "No detailed changes available for repair (try again)", "distributed": 0}

    # Aplicar configuración Guard: excluir colores si corresponde y priorizar preferidos
    excluded_ids = set(guard_config.get('excludedColorIds') or []) if guard_config.get('excludeColor') else set()
    preferred_ids = set(guard_config.get('preferredColorIds') or []) if guard_config.get('preferColor') else set()

    def _expected_color(ch):
        return ch.get('expectedColor', ch.get('color', 0))

    # Filtrar excluidos
    filtered_changes = [ch for ch in changes if _expected_color(ch) not in excluded_ids]

    # Ordenar: primero missing, luego preferidos, luego resto
    def _priority_key(ch):
        color = _expected_color(ch)
        # 'incorrect' cuenta como missing a efectos de prioridad
        is_missing_or_incorrect = 0 if ch.get('type') in ('missing', 'incorrect') else 1
        is_preferred = 0 if color in preferred_ids else 1
        return (is_missing_or_incorrect, is_preferred)

    filtered_changes.sort(key=_priority_key)
    
    # Get all connected slaves (including favorite)
    available_slaves = list(connected_slaves.items())
    
    if not available_slaves:
        raise HTTPException(status_code=400, detail="No available slaves for repair work")
    
    # Distribute changes among available slaves (usar lista filtrada) con reparto round-robin
    work_list = filtered_changes
    if not work_list:
        return {"ok": True, "message": "No eligible pixels to repair after filters", "distributed": 0}

    # Crear buckets por slave y repartir round-robin para minimizar mensajes y balancear carga
    buckets: Dict[str, List[Dict[str, Any]] ] = { sid: [] for sid, _ in available_slaves }
    for i, ch in enumerate(work_list):
        sid, _sinfo = available_slaves[i % len(available_slaves)]
        buckets[sid].append(ch)

    distributed_count = 0
    for sid, _sinfo in available_slaves:
        slave_changes = buckets.get(sid, [])
        if not slave_changes:
            continue
        coords = [{'x': c['x'], 'y': c['y']} for c in slave_changes]
        colors = [c.get('expectedColor', c.get('color', 0)) for c in slave_changes]
        await manager.send_to_slave(sid, {
            "type": "repairOrder",
            "coords": coords,
            "colors": colors,
            "source": "guard_analysis",
            "total_repairs": len(slave_changes)
        })
        distributed_count += len(slave_changes)
        logger.info(f"Sent {len(slave_changes)} repair orders to slave {sid}")
    
    return {
        "ok": True, 
        "message": f"Distributed {distributed_count} repair orders to {len(available_slaves)} slaves",
        "distributed": distributed_count,
        "slaves_used": len(available_slaves)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)