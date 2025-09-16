"""Módulo de compresión y descompresión de mensajes WebSocket.

Este módulo proporciona funciones para comprimir y descomprimir mensajes JSON
que se envían a través de WebSocket, optimizando el ancho de banda para mensajes grandes
manteniendo la latencia baja para mensajes críticos.

Funcionalidades:
- Compresión automática de mensajes grandes (>20MB)
- Exclusión de tipos críticos de latencia (paintBatch, repairOrder)
- Descompresión transparente de mensajes comprimidos
- Manejo robusto de errores
"""

import json
import gzip
import base64
import logging
from typing import Dict, Any, Set

logger = logging.getLogger(__name__)

# Configuración de compresión
COMPRESSION_THRESHOLD = 5 * 1024 * 1024  # 5MB (bytes)

# Tipos que nunca deben comprimirse (órdenes de pintado / control latencia-crítica)
NO_COMPRESS_TYPES: Set[str] = {
    'paintBatch',
    'repairOrder'
}


def _compress_if_needed(message: Dict[str, Any]) -> str:
    """Devuelve JSON (posiblemente envuelto y comprimido) listo para send_text.
    
    Args:
        message: Diccionario del mensaje a comprimir
        
    Returns:
        String JSON listo para envío por WebSocket
        
    Wrapper format:
        {
            type: '__compressed__',
            encoding: 'gzip+base64',
            originalType: str,
            originalLength: int,
            compressedLength: int,
            payload: str
        }
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


def _try_decompress(message: Dict[str, Any]) -> Dict[str, Any]:
    """Si el dict es un wrapper comprimido lo descomprime, si no lo deja igual.
    
    Args:
        message: Diccionario del mensaje a descomprimir
        
    Returns:
        Diccionario descomprimido o el original si no estaba comprimido
    """
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