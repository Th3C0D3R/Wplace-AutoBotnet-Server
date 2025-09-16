"""WPlace Master Server Package.

Este paquete contiene todos los módulos del servidor maestro de WPlace,
organizados de forma modular para facilitar el mantenimiento y desarrollo.

Módulos principales:
- main: Punto de entrada principal de la aplicación
- models: Modelos de datos Pydantic y SQLAlchemy
- storage: Gestión de estado en memoria y configuración
- compression: Compresión/descompresión de mensajes WebSocket
- connection_manager: Gestión de conexiones WebSocket
- pixel_patterns: Algoritmos de selección y ordenamiento de píxeles
- endpoints: Endpoints HTTP y WebSocket principales
- session_orchestrator: Orquestación de sesiones de trabajo
- repair_endpoints: Endpoints de distribución de reparaciones
"""

__version__ = "1.0.0"
__author__ = "WPlace Team"