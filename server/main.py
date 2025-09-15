from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import gzip
import base64
import asyncio
import uuid
from datetime import datetime
import random
import math
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

COMPRESSION_THRESHOLD = 20 * 1024 * 1024  # 20MB (bytes)
# Tipos que nunca deben comprimirse (órdenes de pintado / control latencia-crítica)
NO_COMPRESS_TYPES = {
    'paintBatch',
    'repairOrder'
}

def _compress_if_needed(message: dict) -> str:
    """Devuelve JSON (posiblemente envuelto y comprimido) listo para send_text.
    Wrapper: { type: '__compressed__', encoding: 'gzip+base64', originalType, originalLength, compressedLength, payload }
    """
    try:
        if not isinstance(message, dict) or message.get('type') == '__compressed__':
            return json.dumps(message)
        # Saltar compresión para tipos críticos
        if message.get('type') in NO_COMPRESS_TYPES:
            return json.dumps(message, separators=(',', ':'), ensure_ascii=False)
        raw = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        if len(raw) < COMPRESSION_THRESHOLD:
            return raw.decode('utf-8')
        comp = gzip.compress(raw)
        b64 = base64.b64encode(comp).decode('ascii')
        wrapper = {
            'type': '__compressed__',
            'encoding': 'gzip+base64',
            'originalType': message.get('type'),
            'originalLength': len(raw),
            'compressedLength': len(b64),
            'payload': b64
        }
        return json.dumps(wrapper, separators=(',', ':'), ensure_ascii=False)
    except Exception as e:
        logger.error(f"Compression error: {e}")
        try:
            return json.dumps(message)
        except Exception:
            return '{}'

def _try_decompress(message: dict) -> dict:
    """Si el dict es un wrapper comprimido lo descomprime, si no lo deja igual."""
    try:
        if not isinstance(message, dict):
            return message
        if message.get('type') != '__compressed__' or message.get('encoding') != 'gzip+base64':
            return message
        b64 = message.get('payload')
        if not isinstance(b64, str):
            return message
        raw = base64.b64decode(b64)
        decompressed = gzip.decompress(raw)
        inner = json.loads(decompressed.decode('utf-8'))
        return inner
    except Exception as e:
        logger.error(f"Decompression failed: {e}")
        return message

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
    "colorThreshold": 10,
    "colorComparisonMethod": "rgb",  # nuevo: 'rgb' o 'lab'
    "recentLockSeconds": 60,  # nuevo: TTL de bloqueo tras pintar (segundos)
}
websocket_connections: Dict[str, WebSocket] = {}
ui_connections: List[WebSocket] = []
active_protect_loops: Dict[str, Dict[str, Any]] = {}
# Último guardData subido, para reenviarlo a un nuevo favorito si cambia
last_guard_upload: Optional[Dict[str, Any]] = None
# Selección de slaves a nivel UI (persistente en memoria; usado como default cross-device)
ui_selected_slaves: List[str] = []

# Píxeles recientemente reparados (para evitar repintar durante un periodo fijo de tiempo)
# Antes: basado en número de previews (TTL=5). Ahora: basado en tiempo (60 segundos).
RECENT_LOCK_SECONDS = 60  # valor por defecto; se puede sobrescribir con guard_config['recentLockSeconds']
recently_repaired: Dict[str, float] = {}  # almacena epoch de expiración (segundos)
_recent_lock = Lock()

def _mk_key(x: Any, y: Any) -> str:
    try:
        return f"{int(x)},{int(y)}"
    except Exception:
        return f"{x},{y}"

def mark_recent_repairs(coords: List[Dict[str, Any]]):
    """Marcar coordenadas como bloqueadas hasta ahora + RECENT_LOCK_SECONDS."""
    if not coords:
        return
    now = datetime.utcnow().timestamp()
    # Permitir override por config
    try:
        lock_secs = float(guard_config.get('recentLockSeconds', RECENT_LOCK_SECONDS))
    except Exception:
        lock_secs = float(RECENT_LOCK_SECONDS)
    with _recent_lock:
        for p in coords:
            k = _mk_key(p.get('x'), p.get('y'))
            recently_repaired[k] = now + lock_secs

def age_recent_repairs():
    """Limpia entradas expiradas según tiempo actual."""
    now = datetime.utcnow().timestamp()
    with _recent_lock:
        to_del = [k for k, exp in recently_repaired.items() if float(exp) <= now]
        for k in to_del:
            try:
                del recently_repaired[k]
            except Exception:
                pass

def is_locked_change(change: Dict[str, Any]) -> bool:
    """Devuelve True si la coord está bloqueada aún (no ha expirado). Limpia expirados on-the-fly."""
    try:
        x = change.get('x'); y = change.get('y')
        if x is None or y is None:
            return False
        k = _mk_key(x, y)
        now = datetime.utcnow().timestamp()
        with _recent_lock:
            exp = recently_repaired.get(k)
            if not exp:
                return False
            if float(exp) <= now:
                # Expirado: limpiar y no bloquear
                try:
                    del recently_repaired[k]
                except Exception:
                    pass
                return False
            return True
    except Exception:
        return False

# =====================
# Selección por PATRONES (paridad con Guard)
# =====================
def _bbox(changes: List[Dict[str, Any]]):
    min_x = math.inf; max_x = -math.inf; min_y = math.inf; max_y = -math.inf
    for ch in changes:
        try:
            x = int(ch.get('x')); y = int(ch.get('y'))
        except Exception:
            continue
        if x < min_x: min_x = x
        if x > max_x: max_x = x
        if y < min_y: min_y = y
        if y > max_y: max_y = y
    return (min_x, max_x, min_y, max_y)

def _line_up(changes: List[Dict[str, Any]]):
    rows: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        rows[int(ch['y'])].append(ch)
    out = []
    for y in sorted(rows.keys()):
        row = rows[y]
        row.sort(key=lambda c: int(c['x']))
        out.extend(row)
    return out

def _line_down(changes: List[Dict[str, Any]]):
    rows: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        rows[int(ch['y'])].append(ch)
    out = []
    for y in sorted(rows.keys(), reverse=True):
        row = rows[y]
        row.sort(key=lambda c: int(c['x']))
        out.extend(row)
    return out

def _line_left(changes: List[Dict[str, Any]]):
    cols: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        cols[int(ch['x'])].append(ch)
    out = []
    for x in sorted(cols.keys()):
        col = cols[x]
        col.sort(key=lambda c: int(c['y']))
        out.extend(col)
    return out

def _line_right(changes: List[Dict[str, Any]]):
    cols: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        cols[int(ch['x'])].append(ch)
    out = []
    for x in sorted(cols.keys(), reverse=True):
        col = cols[x]
        col.sort(key=lambda c: int(c['y']))
        out.extend(col)
    return out

def _center(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    return sorted(changes, key=lambda c: math.hypot(int(c['x']) - cx, int(c['y']) - cy))

def _borders(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    def ring(c):
        x = int(c['x']); y = int(c['y'])
        return min(x - min_x, max_x - x, y - min_y, max_y - y)
    return sorted(changes, key=ring)

def _zigzag(changes: List[Dict[str, Any]]):
    rows: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        rows[int(ch['y'])].append({**ch, '_x': int(ch['x'])})
    out = []
    for i, y in enumerate(sorted(rows.keys())):
        row = rows[y]
        row.sort(key=lambda c: c['_x'], reverse=(i % 2 == 1))
        out.extend(row)
    for c in out:
        c.pop('_x', None)
    return out

def _diagonal(changes: List[Dict[str, Any]]):
    return sorted(changes, key=lambda c: (int(c['x']) + int(c['y']), int(c['x'])))

def _snake(changes: List[Dict[str, Any]]):
    # similar a zigzag
    return _zigzag(changes)

def _diagonal_sweep(changes: List[Dict[str, Any]]):
    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        s = int(ch['x']) + int(ch['y'])
        groups[s].append(ch)
    out = []
    for s in sorted(groups.keys()):
        g = groups[s]
        g.sort(key=lambda c: int(c['x']))
        out.extend(g)
    return out

def _spiral_like(changes: List[Dict[str, Any]], clockwise: Optional[bool] = None):
    # aproximación: ordenar por distancia al centro y luego por ángulo (ajustado por sentido si se indica)
    min_x, max_x, min_y, max_y = _bbox(changes)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    arr = []
    for ch in changes:
        x = int(ch['x']); y = int(ch['y'])
        dx = x - cx; dy = y - cy
        r = math.hypot(dx, dy)
        ang = math.atan2(dy, dx)
        if clockwise is True:
            ang = ang
        elif clockwise is False:
            ang = -ang
        arr.append((r, ang, ch))
    arr.sort(key=lambda t: (round(t[0], 3), t[1]))
    return [t[2] for t in arr]

def _cluster(changes: List[Dict[str, Any]]):
    if not changes:
        return []
    seed = random.choice(changes)
    sx = int(seed['x']); sy = int(seed['y'])
    return sorted(changes, key=lambda c: math.hypot(int(c['x']) - sx, int(c['y']) - sy))

def _wave(changes: List[Dict[str, Any]]):
    if not changes:
        return []
    min_x, max_x, _min_y, _max_y = _bbox(changes)
    width = max(1, (max_x - min_x))
    def metric(c):
        x = int(c['x']); y = int(c['y'])
        nx = (x - min_x) / width
        wave_y = math.sin(nx * math.pi * 2) * 10
        return (abs(y - wave_y), x)
    return sorted(changes, key=metric)

def _corners(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    corners = [(min_x, min_y), (max_x, min_y), (min_x, max_y), (max_x, max_y)]
    def dist_to_nearest_corner(c):
        x = int(c['x']); y = int(c['y'])
        return min(math.hypot(x - cx, y - cy) for (cx, cy) in corners)
    return sorted(changes, key=dist_to_nearest_corner)

def _sweep(changes: List[Dict[str, Any]]):
    sections: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        x = int(ch['x']); y = int(ch['y'])
        sections[(x // 8, y // 8)].append(ch)
    out = []
    for key in sorted(sections.keys(), key=lambda k: (k[1], k[0])):
        out.extend(sections[key])
    return out

def _priority(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    def score(c):
        x = int(c['x']); y = int(c['y'])
        center_d = math.hypot(x - cx, y - cy)
        edge_d = min(x - min_x, max_x - x, y - min_y, max_y - y)
        rand = random.random() * 0.3
        return center_d * 0.4 - edge_d * 0.3 + rand
    return sorted(changes, key=score)

def _proximity(changes: List[Dict[str, Any]]):
    if not changes:
        return []
    remaining = changes[:]
    start = random.choice(remaining)
    out = [start]
    remaining.remove(start)
    def dist(a, b):
        return math.hypot(int(a['x']) - int(b['x']), int(a['y']) - int(b['y']))
    while remaining:
        last = out[-1]
        nxt = min(remaining, key=lambda c: dist(c, last))
        out.append(nxt)
        remaining.remove(nxt)
    return out

def _quadrant(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    quads = [[], [], [], []]
    for ch in changes:
        x = int(ch['x']); y = int(ch['y'])
        if x <= cx and y <= cy:
            quads[0].append(ch)
        elif x > cx and y <= cy:
            quads[1].append(ch)
        elif x <= cx and y > cy:
            quads[2].append(ch)
        else:
            quads[3].append(ch)
    out = []
    idxs = [0, 0, 0, 0]
    while True:
        progressed = False
        for i in range(4):
            if idxs[i] < len(quads[i]):
                out.append(quads[i][idxs[i]])
                idxs[i] += 1
                progressed = True
        if not progressed:
            break
    return out

def _scattered(changes: List[Dict[str, Any]]):
    out: List[Dict[str, Any]] = []
    cand = changes[:]
    if not cand:
        return out
    # empezar por uno al azar
    out.append(cand.pop(random.randrange(len(cand))))
    def min_dist_to_out(c):
        return min(math.hypot(int(c['x']) - int(o['x']), int(c['y']) - int(o['y'])) for o in out)
    while cand:
        best_idx = max(range(len(cand)), key=lambda i: min_dist_to_out(cand[i]))
        out.append(cand.pop(best_idx))
    return out

def _diagonal_weight(ch, min_x, max_x, min_y, max_y):
    x = int(ch['x']); y = int(ch['y'])
    dist_to_left = x - min_x
    dist_to_right = max_x - x
    dist_to_top = y - min_y
    dist_to_bottom = max_y - y
    return min(dist_to_left, dist_to_right, dist_to_top, dist_to_bottom)

def _biased_random(changes: List[Dict[str, Any]]):
    if not changes:
        return []
    min_x, max_x, min_y, max_y = _bbox(changes)
    items = []
    for ch in changes:
        w = 1.0 / (_diagonal_weight(ch, min_x, max_x, min_y, max_y) + 1.0) + random.random() * 0.5
        items.append((w, ch))
    # Selección ponderada en orden descendente
    items.sort(key=lambda t: t[0], reverse=True)
    return [c for _w, c in items]

def _anchor_points(changes: List[Dict[str, Any]]):
    min_x, max_x, min_y, max_y = _bbox(changes)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    anchors = [
        (min_x, min_y, 1), (max_x, min_y, 1), (min_x, max_y, 1), (max_x, max_y, 1),
        (cx, cy, 2), (cx, min_y, 3), (cx, max_y, 3), (min_x, cy, 3), (max_x, cy, 3)
    ]
    def key(c):
        x = int(c['x']); y = int(c['y'])
        best_p = 10; best_d = math.inf
        for ax, ay, pr in anchors:
            d = math.hypot(x - ax, y - ay)
            if d < best_d:
                best_d = d; best_p = pr
        return (best_p, best_d)
    return sorted(changes, key=key)

def select_pixels_by_pattern(pattern: str, changes: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    # Clonar para no mutar
    pool = [c for c in changes if isinstance(c, dict) and 'x' in c and 'y' in c]
    if not pool or count <= 0:
        return []
    p = (pattern or 'random')
    try:
        if p == 'lineUp':
            ordered = _line_up(pool)
        elif p == 'lineDown':
            ordered = _line_down(pool)
        elif p == 'lineLeft':
            ordered = _line_left(pool)
        elif p == 'lineRight':
            ordered = _line_right(pool)
        elif p == 'center':
            ordered = _center(pool)
        elif p == 'borders':
            ordered = _borders(pool)
        elif p == 'spiral':
            ordered = _spiral_like(pool, None)
        elif p == 'spiralClockwise':
            ordered = _spiral_like(pool, True)
        elif p == 'spiralCounterClockwise':
            ordered = _spiral_like(pool, False)
        elif p == 'zigzag':
            ordered = _zigzag(pool)
        elif p == 'diagonal':
            ordered = _diagonal(pool)
        elif p == 'cluster':
            ordered = _cluster(pool)
        elif p == 'wave':
            ordered = _wave(pool)
        elif p == 'corners':
            ordered = _corners(pool)
        elif p == 'sweep':
            ordered = _sweep(pool)
        elif p == 'priority':
            ordered = _priority(pool)
        elif p == 'proximity':
            ordered = _proximity(pool)
        elif p == 'quadrant':
            ordered = _quadrant(pool)
        elif p == 'scattered':
            ordered = _scattered(pool)
        elif p == 'snake':
            ordered = _snake(pool)
        elif p == 'diagonalSweep':
            ordered = _diagonal_sweep(pool)
        elif p == 'biasedRandom':
            ordered = _biased_random(pool)
        elif p == 'anchorPoints':
            ordered = _anchor_points(pool)
        else:
            # random por defecto
            ordered = pool[:]
            random.shuffle(ordered)
    except Exception:
        ordered = pool[:]
        random.shuffle(ordered)
    return ordered[:count]

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
                    payload = {"type": "guardConfig", "config": guard_config, "timestamp": datetime.utcnow().isoformat()}
                    await self.slave_connections[slave_id].send_text(_compress_if_needed(payload))
                except Exception as _e:
                    logger.error(f"Error sending guard config to first favorite {slave_id}: {_e}")
                # Enviar guardData si existe para continuar preview
                try:
                    if last_guard_upload:
                        payload = {
                            "type": "guardData",
                            "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                            "guardData": last_guard_upload.get("data", {}),
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        await self.slave_connections[slave_id].send_text(_compress_if_needed(payload))
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
                    payload = {"type": "guardConfig", "config": guard_config, "timestamp": datetime.utcnow().isoformat()}
                    await self.slave_connections[slave_id].send_text(_compress_if_needed(payload))
                except Exception as _e:
                    logger.error(f"Error re-sending guard config to favorite {slave_id}: {_e}")
                # También re-enviar guardData si existe
                try:
                    if last_guard_upload:
                        payload = {
                            "type": "guardData",
                            "filename": last_guard_upload.get("filename", "uploaded_guard.json"),
                            "guardData": last_guard_upload.get("data", {}),
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        await self.slave_connections[slave_id].send_text(_compress_if_needed(payload))
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
                await self.slave_connections[slave_id].send_text(_compress_if_needed(message))
            except Exception as e:
                logger.error(f"Error sending to slave {slave_id}: {e}")
                await self.disconnect_slave(slave_id)

    async def broadcast_to_ui(self, message: dict):
        disconnected = []
        for connection in self.ui_connections:
            try:
                # Suponemos que la UI no necesita recibir >20MB; aun así aplicamos compresión defensiva
                await connection.send_text(_compress_if_needed(message))
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
    colorThreshold: Optional[int] = None
    colorComparisonMethod: Optional[str] = None  # 'rgb' | 'lab'
    recentLockSeconds: Optional[int] = None  # TTL de bloqueo tras pintar (segundos)

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
    # watchMode eliminado: no se envía toggleWatch
    return {"ok": True, "requested": target_id}

## Endpoint toggle-watch eliminado (watchMode deprecado)

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
                try:
                    # 1. Slaves válidos
                    current_valid_slaves = [sid for sid in session.slave_ids if sid in connected_slaves]
                    if not current_valid_slaves:
                        await asyncio.sleep(3); continue

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
                            logger.warning(f"[orchestrate_loop] Ignorando {len(bad)} cambios no dict (tipos={ {type(b).__name__ for b in bad} })")
                            changes = [c for c in changes if isinstance(c, dict)]

                    # 3. Cargas
                    charges: Dict[str, int] = {}
                    total_remaining = 0
                    for sid in current_valid_slaves:
                        try:
                            rem = int((connected_slaves[sid].telemetry or {}).get('remaining_charges') or 0)
                        except Exception:
                            rem = 0
                        charges[sid] = rem; total_remaining += rem

                    if not changes:
                        await asyncio.sleep(5); continue
                    if total_remaining <= 0:
                        await asyncio.sleep(30); continue

                    # 4. Planificación
                    pixels_per_batch = int(guard_config.get('pixelsPerBatch') or 10)
                    spend_all = bool(guard_config.get('spendAllPixelsOnStart'))
                    round_total = sum(charges.values()) if spend_all else min(sum(charges.values()), pixels_per_batch)
                    if round_total <= 0:
                        await asyncio.sleep(5); continue

                    plan: Dict[str, int] = { sid: 0 for sid in current_valid_slaves }
                    order = [sid for sid in current_valid_slaves if charges.get(sid, 0) > 0]
                    idx = 0; assigned = 0
                    while assigned < round_total and order:
                        sid = order[idx % len(order)]
                        if plan[sid] < charges[sid]:
                            plan[sid] += 1; assigned += 1
                        idx += 1
                        if all(plan[s] >= charges[s] for s in order) and assigned < round_total:
                            break

                    try:
                        changes = [ch for ch in changes if not is_locked_change(ch)]
                    except Exception:
                        pass
                    pick = min(len(changes), sum(plan.values()))
                    if pick <= 0:
                        await asyncio.sleep(5); continue

                    try:
                        selected = select_pixels_by_pattern(str(guard_config.get('protectionPattern', 'random')), changes, pick)
                    except Exception:
                        selected = changes[:pick]

                    # 5. Agrupar y construir colas
                    TILE = 1000
                    queues: Dict[str, List[Dict[str, Any]]] = { sid: [] for sid in current_valid_slaves }
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
                                x = int(ch.get('x')); y = int(ch.get('y'))
                            except Exception:
                                continue
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
                            await send_sub(sid, items)

                    deadline = asyncio.get_event_loop().time() + 90.0
                    while asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(0.3)
                        if batch_tracker.get_pending(req_id) == 0:
                            break
                        fails = batch_tracker.failed_assignments(req_id)
                        for (sid, key), data in fails:
                            candidates = [x for x in current_valid_slaves if x != sid and charges.get(x, 0) > 0] or [x for x in current_valid_slaves if x != sid]
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
    # Evitar píxeles bloqueados por reparaciones recientes
    try:
        changes = [c for c in changes if not is_locked_change(c)]
    except Exception:
        pass
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
    # Aplicar patrón de protección
    try:
        selected = select_pixels_by_pattern(str(guard_config.get('protectionPattern', 'random')), changes, pick)
    except Exception:
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
        await websocket.send_text(_compress_if_needed({
            "type": "connected",
            "slave_id": slave_id
        }))
        
        # Notificar al slave si es favorito
        if connected_slaves[slave_id].is_favorite:
            await websocket.send_text(_compress_if_needed({
                "type": "favorite_status",
                "is_favorite": True
            }))
        
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            # Intentar descomprimir si es wrapper
            message = _try_decompress(message)
            
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
                        # Cada preview del favorito envejece los bloqueos
                        try:
                            age_recent_repairs()
                        except Exception:
                            pass
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
                        # autoDistribute eliminado
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
                    # Si la pintura fue OK, marcar coords como reparadas recientemente (TTL de previews)
                    try:
                        if bool(message.get('ok')) and coords:
                            mark_recent_repairs(coords)
                    except Exception:
                        pass
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

        # Hydrate available colors for UI initial state
        def _normalize_colors(arr):
            out = []
            try:
                for i, c in enumerate(arr or []):
                    if isinstance(c, dict):
                        cid = c.get('id', i)
                        r = int(c.get('r', 0))
                        g = int(c.get('g', 0))
                        b = int(c.get('b', 0))
                        out.append({ 'id': cid, 'r': r, 'g': g, 'b': b })
            except Exception:
                pass
            return out

        initial_available_colors = []
        # 1) Prefer colors from favorite's preview_data
        try:
            fav_id = next((sid for sid, s in connected_slaves.items() if getattr(s, 'is_favorite', False)), None)
            if fav_id:
                fav = connected_slaves.get(fav_id)
                if fav and isinstance(fav.telemetry, dict):
                    pd = fav.telemetry.get('preview_data') or {}
                    initial_available_colors = _normalize_colors(pd.get('availableColors') or [])
        except Exception:
            initial_available_colors = []
        # 2) If empty, search any slave's preview_data
        if not initial_available_colors:
            try:
                for s in connected_slaves.values():
                    if isinstance(s.telemetry, dict):
                        pd = s.telemetry.get('preview_data') or {}
                        colors = _normalize_colors(pd.get('availableColors') or [])
                        if colors:
                            initial_available_colors = colors
                            break
            except Exception:
                pass
        # 3) If still empty, try last guard upload (colors field)
        if not initial_available_colors:
            try:
                if last_guard_upload and isinstance(last_guard_upload.get('data'), dict):
                    initial_available_colors = _normalize_colors(last_guard_upload['data'].get('colors') or [])
            except Exception:
                pass
        await websocket.send_text(json.dumps({
            "type": "initial_state",
            "slaves": slaves_data,
            "projects": projects_list,
            "sessions": sessions_list,
            "selected_slaves": list(ui_selected_slaves),
            "available_colors": initial_available_colors
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
    # Además, filtrar píxeles recientemente reparados
    def _not_locked(p):
        try:
            return not is_locked_change({'x': p.get('x'), 'y': p.get('y')})
        except Exception:
            return True
    incoming = [p for p in order.pixels if _not_locked(p)]
    high_priority = [p for p in incoming if p.get('priority') == 'high']
    medium_priority = [p for p in incoming if p.get('priority') == 'medium']
    low_priority = [p for p in incoming if p.get('priority') not in ['high', 'medium']]
    
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