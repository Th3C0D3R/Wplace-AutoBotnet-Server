#!/usr/bin/env bash
# start.sh — Inicia/actualiza el WPlace Master Server en local
# Requisitos: docker, docker-compose
# Uso: ejecutar desde esta carpeta (wplace-masterserver/). Idempotente: reconstruye y reinicia servicios.

set -euo pipefail

# Opciones
FRONTEND_ONLY=0
SYNC_ONLY=0
REBUILD=1

# Colores simples
INFO="\033[1;34m[INFO]\033[0m"
WARN="\033[1;33m[WARN]\033[0m"
ERR="\033[1;31m[ERR ]\033[0m"
OK="\033[1;32m[ OK ]\033[0m"

# Parseo de argumentos
for arg in "$@"; do
  case "$arg" in
    --frontend-only)
      FRONTEND_ONLY=1
      ;;
    -n|--no-rebuild|--sync-only)
      SYNC_ONLY=1
      REBUILD=0
      ;;
    -h|--help)
      echo "Uso: $0 [OPCIONES]"
      echo "Opciones:"
      echo "  --frontend-only    Iniciar/actualizar solo el frontend (servicio ui)"
      echo "  -n, --no-rebuild   No reconstruir imágenes, solo reiniciar servicios"
      echo "  -h, --help         Mostrar esta ayuda"
      echo ""
      echo "Ejemplos:"
      echo "  $0                 # Iniciar todos los servicios (por defecto)"
      echo "  $0 --frontend-only # Iniciar solo el frontend"
      echo "  $0 --no-rebuild    # Reiniciar sin reconstruir"
      exit 0
      ;;
    *)
      echo "Opción desconocida: $arg" 1>&2
      echo "Usa --help para ver las opciones disponibles" 1>&2
      exit 1
      ;;
  esac
done

# Ir al directorio del script
cd "$(dirname "$0")"

if [[ $FRONTEND_ONLY -eq 1 ]]; then
    echo -e "$INFO Iniciando WPlace Frontend solamente..."
else
    echo -e "$INFO Iniciando WPlace Master & Slave System..."
fi
echo "======================================"

# Verificar que Docker esté ejecutándose
echo -e "$INFO Verificando Docker..."
if ! docker info > /dev/null 2>&1; then
    echo -e "$ERR Docker no está ejecutándose. Por favor, inicia Docker primero."
    exit 1
fi
echo -e "$OK Docker está ejecutándose"

# Detectar docker-compose vs docker compose
echo -e "$INFO Detectando Docker Compose..."
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    echo -e "$OK Usando 'docker compose' (plugin)"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    echo -e "$OK Usando 'docker-compose' (binario independiente)"
else
    echo -e "$ERR Docker Compose no está disponible. Instálalo y vuelve a ejecutar."
    exit 1
fi

# Crear archivo .env si no existe
echo -e "$INFO Verificando configuración .env..."
if [[ ! -f .env ]]; then
    echo -e "$INFO Creando archivo .env básico para WPlace Master Server..."
    cat > .env << 'EOF_ENV'
# WPlace Master Server Configuration
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
EOF_ENV
    echo -e "$OK Archivo .env creado"
else
    echo -e "$OK Archivo .env ya existe"
fi

# Si se solicitó solo restart sin rebuild, hacerlo ahora
if [[ $SYNC_ONLY -eq 1 ]]; then
    echo -e "$INFO Reiniciando servicios sin reconstruir imágenes..."
    if [[ $FRONTEND_ONLY -eq 1 ]]; then
        ${COMPOSE_CMD} restart ui
        echo -e "$OK Frontend reiniciado (sin rebuild)"
    else
        ${COMPOSE_CMD} restart
        echo -e "$OK Todos los servicios reiniciados (sin rebuild)"
    fi
    exit 0
fi

# Construir y levantar servicios
if [[ $FRONTEND_ONLY -eq 1 ]]; then
    echo -e "$INFO Construyendo y levantando servicio frontend..."
    echo -e "$INFO Deteniendo contenedor frontend existente..."
    ${COMPOSE_CMD} stop ui || true
    ${COMPOSE_CMD} rm -f ui || true
    
    echo -e "$INFO Construyendo imagen del frontend..."
    if ! ${COMPOSE_CMD} build --no-cache ui; then
        echo -e "$ERR La construcción del frontend falló. Revisa los logs arriba para más detalles."
        echo -e "$WARN Soluciones comunes:"
        echo "   - Asegurar que Docker tenga suficiente memoria (4GB+ recomendado)"
        echo "   - Verificar conexión a internet para descargas de paquetes"
        echo "   - Intentar: docker system prune -f para limpiar espacio"
        exit 1
    fi
    
    echo -e "$INFO Iniciando contenedor frontend..."
    if ! ${COMPOSE_CMD} up -d ui; then
        echo -e "$ERR Falló al iniciar el contenedor frontend. Verificando logs..."
        ${COMPOSE_CMD} logs ui
        exit 1
    fi
else
    echo -e "$INFO Construyendo y levantando todos los servicios (sin detener base de datos)..."
    # IMPORTANTE: Evitamos bajar todo el stack para prevenir shutdowns rápidos de Postgres y desconexiones WS 1012
    # Construir solo imágenes de apps; redis/postgres usan imágenes oficiales y no requieren build
    echo -e "$INFO Construyendo imágenes (server y ui, esto puede tomar varios minutos)..."
    if ! ${COMPOSE_CMD} build --no-cache server ui; then
        echo -e "$ERR La construcción falló. Revisa los logs arriba para más detalles."
        echo -e "$WARN Soluciones comunes:"
        echo "   - Asegurar que Docker tenga suficiente memoria (4GB+ recomendado)"
        echo "   - Verificar conexión a internet para descargas de paquetes"
        echo "   - Intentar: docker system prune -f para limpiar espacio"
        exit 1
    fi
    
    echo -e "$INFO Iniciando/Actualizando contenedores..."
    # Up solo recrea servicios que cambiaron; Postgres permanece ejecutándose si no cambió
    if ! ${COMPOSE_CMD} up -d server ui redis postgres; then
        echo -e "$ERR Falló al iniciar contenedores. Verificando logs..."
        ${COMPOSE_CMD} logs
        exit 1
    fi
fi

# Esperar a que los servicios estén listos
echo -e "$INFO Esperando a que los servicios inicien..."
sleep 10

# Verificar health de los servicios
echo -e "$INFO Verificando salud de los servicios..."

if [[ $FRONTEND_ONLY -eq 1 ]]; then
    # Verificar solo frontend Astro
    if curl -f http://localhost:3004 > /dev/null 2>&1; then
        echo -e "$OK Frontend Astro ejecutándose en http://localhost:3004"
    else
        echo -e "$WARN Frontend Astro no está respondiendo"
    fi
else
    # Verificar servidor FastAPI
    if curl -f http://localhost:8008/health > /dev/null 2>&1; then
        echo -e "$OK Servidor FastAPI ejecutándose en http://localhost:8008"
    else
        echo -e "$WARN Servidor FastAPI no está respondiendo"
    fi
    
    # Verificar frontend Astro
    if curl -f http://localhost:3004 > /dev/null 2>&1; then
        echo -e "$OK Frontend Astro ejecutándose en http://localhost:3004"
    else
        echo -e "$WARN Frontend Astro no está respondiendo"
    fi
    
    # Verificar Redis
    if ${COMPOSE_CMD} exec -T redis redis-cli ping > /dev/null 2>&1; then
        echo -e "$OK Redis está ejecutándose"
    else
        echo -e "$WARN Redis no está respondiendo"
    fi
    
    # Verificar PostgreSQL
    if ${COMPOSE_CMD} exec -T postgres pg_isready -U wplace > /dev/null 2>&1; then
        echo -e "$OK PostgreSQL está ejecutándose"
    else
        echo -e "$WARN PostgreSQL no está respondiendo"
    fi
fi

echo ""
if [[ $FRONTEND_ONLY -eq 1 ]]; then
    echo -e "$OK WPlace Frontend está listo!"
    echo "======================================"
    echo "📊 Dashboard: http://localhost:3004"
    echo ""
    echo "📋 Siguientes pasos:"
    echo "1. Abre tu navegador y ve a http://localhost:3004"
    echo "2. Navega a https://wplace.live en otra pestaña"
    echo "3. Inyecta el script Auto-Slave.js usando uno de estos métodos:"
    echo "   - Extensión del navegador (recomendado)"
    echo "   - Inyección de bookmarklet"
    echo "   - Inyección manual del script en la consola"
    echo ""
    echo "📜 Ver logs del frontend con: ${COMPOSE_CMD} logs -f ui"
    echo "🛑 Detener frontend con: ${COMPOSE_CMD} stop ui"
else
    echo -e "$OK WPlace Master System está listo!"
    echo "======================================"
    echo "📊 Dashboard: http://localhost:3004"
    echo "🔧 API Docs:  http://localhost:8008/docs"
    echo "📝 API Health: http://localhost:8008/health"
    echo ""
    echo "📋 Siguientes pasos:"
    echo "1. Abre tu navegador y ve a http://localhost:3004"
    echo "2. Navega a https://wplace.live en otra pestaña"
    echo "3. Inyecta el script Auto-Slave.js usando uno de estos métodos:"
    echo "   - Extensión del navegador (recomendado)"
    echo "   - Inyección de bookmarklet"
    echo "   - Inyección manual del script en la consola"
    echo ""
    echo "🔧 Comandos útiles:"
    echo "- Ver logs del servidor: ${COMPOSE_CMD} logs -f server"
    echo "- Ver logs de la UI:     ${COMPOSE_CMD} logs -f ui"
    echo "- Ver todos los logs:    ${COMPOSE_CMD} logs -f"
    echo "- Reiniciar servicios:   ${COMPOSE_CMD} restart"
    echo "- Para actualizar:       vuelve a ejecutar este script"
    echo ""
    echo "📜 Ver logs con: ${COMPOSE_CMD} logs -f"
    echo "🛑 Detener sistema con: ${COMPOSE_CMD} down"
fi
echo ""