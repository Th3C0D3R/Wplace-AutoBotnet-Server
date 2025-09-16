"""WPlace Master Server - Punto de entrada principal.

Este es el archivo principal del servidor maestro que coordina bots de WPlace.
Importa y configura todos los módulos necesarios para proporcionar la funcionalidad
completa del sistema distribuido.

Funcionalidades principales:
- Servidor FastAPI con endpoints REST y WebSocket
- Gestión de slaves conectados y comunicación en tiempo real
- Sistema de proyectos y sesiones de trabajo
- Configuración Guard y análisis de píxeles
- Distribución automática de trabajo de reparación
- Compresión automática de mensajes grandes
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Importar módulos del proyecto
try:
    # Importaciones relativas (cuando se ejecuta como paquete)
    from .models import init_db
    from .endpoints import setup_endpoints
    from .storage import (
        connected_slaves, active_projects, active_sessions, guard_config,
        last_guard_upload, ui_selected_slaves, active_protect_loops,
        batch_tracker, recently_repaired, _last_preview_timestamp
    )
    from .connection_manager import manager
    from .compression import _compress_if_needed, _try_decompress
    from .pixel_patterns import select_pixels_by_pattern
except ImportError:
    # Importaciones absolutas (cuando se ejecuta directamente)
    from models import init_db
    from endpoints import setup_endpoints
    from storage import (
        connected_slaves, active_projects, active_sessions, guard_config,
        last_guard_upload, ui_selected_slaves, active_protect_loops,
        batch_tracker, recently_repaired, _last_preview_timestamp
    )
    from connection_manager import manager
    from compression import _compress_if_needed, _try_decompress
    from pixel_patterns import select_pixels_by_pattern

# Crear aplicación FastAPI
app = FastAPI(title="WPlace Master Server", version="1.0.0")

# Configurar CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurar todos los endpoints
setup_endpoints(app)

# Punto de entrada para ejecutar el servidor
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)