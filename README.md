# WPlace Master Server

<p align="center">
  <strong>🎯 Servidor maestro para coordinar bots de WPlace con interfaz web de gestión</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Astro-FF5D01?style=for-the-badge&logo=astro&logoColor=white" alt="Astro">
  <img src="https://img.shields.io/badge/PostgreSQL-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white" alt="Redis">
</p>

## ☕ Apoyo al desarrollo

Si este proyecto te ha sido útil, considera apoyar su desarrollo:

<p align="center">
  <a href="https://buymeacoffee.com/alarisco">
    <img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee">
  </a>
</p>

---

## 📋 Descripción

WPlace Master Server es un sistema de coordinación centralizado que permite gestionar múltiples bots de WPlace de forma eficiente. Proporciona una interfaz web moderna para controlar bots esclavos, configurar proyectos de protección (Guard) y pintura automática (Image), y monitorear el estado en tiempo real.

### ✨ Características principales

- 🤖 **Gestión de bots esclavos**: Conecta y coordina múltiples instancias de bots
- 🎨 **Modo Image**: Automatización para crear pixel art desde imágenes
- 🛡️ **Modo Guard**: Protección automática de áreas específicas del canvas
- 🌐 **Interfaz web moderna**: Panel de control intuitivo construido con Astro y React
- 📊 **Monitoreo en tiempo real**: WebSockets para actualizaciones instantáneas
- 🔄 **Compresión inteligente**: Optimización automática de mensajes grandes
- 📈 **Telemetría avanzada**: Estadísticas detalladas de rendimiento

---

## 🚀 Inicio rápido

### Método 1: Script de inicio automático (Recomendado)

El proyecto incluye un script <mcfile name="start.sh" path="/Users/alvaroalonso/workspace/project-place/wplace-masterserver/start.sh"></mcfile> que simplifica el proceso de inicio:

```bash
# Hacer el script ejecutable
chmod +x start.sh

# Iniciar todo el sistema
./start.sh

# Solo iniciar el frontend (útil para desarrollo)
./start.sh --frontend-only

# Ver ayuda
./start.sh --help
```

**Características del script:**
- ✅ Verificación automática de Docker
- ✅ Creación automática del archivo `.env`
- ✅ Construcción e inicio de contenedores
- ✅ Verificación de salud de servicios
- ✅ Modo solo-frontend para desarrollo
- ✅ Mensajes informativos y solución de problemas

### Método 2: Docker Compose manual

Si prefieres control manual sobre el proceso:

### Prerrequisitos

- 🐳 **Docker** y **Docker Compose** instalados
- 🌐 **Puerto 3004** (interfaz web) y **8008** (API) disponibles
- 💾 Al menos **2GB de RAM** disponible para los contenedores

### Instalación

```bash
# 1. Clonar el repositorio
git clone <repository-url>
cd wplace-masterserver

# 2. Usar el script de inicio (recomendado)
chmod +x start.sh
./start.sh
```

### 2. Despliegue local (desarrollo)

```bash
# Crear archivo de configuración
cat > .env << EOF
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
EOF

# Construir e iniciar servicios
docker-compose up -d --build

# Ver logs en tiempo real
docker-compose logs -f

# Verificar que todo funciona
docker-compose ps
```

**Acceso:**
- 🌐 **Interfaz web**: http://localhost:3004
- 🔧 **API del servidor**: http://localhost:8008
- 📊 **Base de datos PostgreSQL**: localhost:5432 (usuario: `wplace`, contraseña: `wplace123`, db: `wplace_master`)
- 🗄️ **Redis**: localhost:6379

### 3. Despliegue en producción

Para despliegues en servidores remotos, utiliza el script automatizado:

```bash
# Hacer ejecutable el script
chmod +x despliegue.sh

# Ejecutar despliegue
./despliegue.sh
```

El script te pedirá:
- IP o dominio del servidor
- Usuario SSH
- Contraseña SSH
- Ruta de instalación (por defecto: `/opt/wplace-masterserver`)

---

## 🔒 Configuración SSL (Importante para bots no-localhost)

> ⚠️ **ADVERTENCIA**: Si planeas usar bots que NO sean localhost, DEBES configurar certificados SSL válidos. Los navegadores modernos bloquean conexiones WebSocket no seguras desde sitios HTTPS.

### Opción 1: Nginx Proxy Manager (Recomendado)

**Nginx Proxy Manager** es la forma más sencilla de gestionar certificados SSL automáticamente:

1. **Instala Nginx Proxy Manager:**
```bash
# Crear directorio para NPM
mkdir nginx-proxy-manager
cd nginx-proxy-manager

# Descargar docker-compose.yml de NPM
curl -o docker-compose.yml https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/main/docker-compose.yml

# Iniciar NPM
docker-compose up -d
```

2. **Configurar el proxy:**
   - Accede a `http://tu-servidor:81`
   - Login inicial: `admin@example.com` / `changeme`
   - Crea un nuevo "Proxy Host":
     - **Domain**: `tu-dominio.com`
     - **Forward Hostname/IP**: `tu-servidor-ip`
     - **Forward Port**: `8008` (puerto del WPlace Master Server)
   - En la pestaña "SSL":
     - Marca "Request a new SSL Certificate"
     - Marca "Force SSL"
     - Acepta los términos de Let's Encrypt

3. **Ventajas de NPM:**
   - ✅ Renovación automática de certificados
   - ✅ Interfaz web intuitiva
   - ✅ Soporte para múltiples dominios
   - ✅ Configuración de proxy reverso automática

### Opción 2: Let's Encrypt manual

Si prefieres configurar Let's Encrypt manualmente:

```bash
# Instalar Certbot
sudo apt install certbot  # Ubuntu/Debian
# brew install certbot    # macOS

# Generar certificados
sudo certbot certonly --standalone -d tu-dominio.com

# Copiar certificados
sudo cp /etc/letsencrypt/live/tu-dominio.com/fullchain.pem ./certs/
sudo cp /etc/letsencrypt/live/tu-dominio.com/privkey.pem ./certs/
sudo chown $USER:$USER ./certs/*
```

### Opción 3: Certificados autofirmados (Solo para desarrollo)

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/privkey.pem -out certs/fullchain.pem -days 365 -nodes -subj "/C=ES/ST=State/L=City/O=Organization/CN=localhost"
```

### Configuración en Docker Compose

Para usar certificados locales, descomenta las líneas SSL en `docker-compose.yml`:

```yaml
server:
  volumes:
    - ./certs:/app/certs  # Descomenta esta línea
  environment:
    - SSL_CERT_PATH=/app/certs/fullchain.pem   # Descomenta
    - SSL_KEY_PATH=/app/certs/privkey.pem      # Descomenta
```

---

## 🏗️ Arquitectura del sistema

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Interfaz Web  │    │  Master Server  │    │   Bot Esclavo   │
│   (Astro/React) │◄──►│    (FastAPI)    │◄──►│   (WebSocket)   │
│   Puerto 3004   │    │   Puerto 8008   │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   PostgreSQL    │
                    │ wplace_master   │
                    │   + Redis       │
                    └─────────────────┘
```

### Componentes principales

- **Master Server**: API FastAPI con WebSockets para coordinación
- **Interfaz Web**: Panel de control construido con Astro y React
- **PostgreSQL**: Base de datos para persistencia de configuraciones
- **Redis**: Cache y gestión de sesiones en tiempo real

---

## 🔧 Configuración avanzada

### Variables de entorno

Crea un archivo `.env` en la raíz del proyecto:

```env
# Base de datos
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master

# Redis
REDIS_URL=redis://redis:6379

# SSL (solo para producción)
SSL_CERT_PATH=/app/certs/fullchain.pem
SSL_KEY_PATH=/app/certs/privkey.pem

# Configuración del servidor
PYTHONUNBUFFERED=1
```

### Personalización de puertos

Modifica el <mcfile name="docker-compose.yml" path="/Users/alvaroalonso/workspace/project-place/wplace-masterserver/docker-compose.yml"></mcfile> según tus necesidades:

```yaml
services:
  server:
    ports:
      - "8008:8000"  # Puerto actual del servidor
  ui:
    ports:
      - "3004:3000"  # Puerto actual de la interfaz web
  postgres:
    ports:
      - "5432:5432"  # Puerto de PostgreSQL
  redis:
    ports:
      - "6379:6379"  # Puerto de Redis
```

---

## 📖 Uso del sistema

### 1. Conectar bots esclavos

Los bots se conectan automáticamente al master server mediante WebSocket:

```
ws://tu-servidor:8008/ws/slave/{slave_id}
```

### 2. Configurar proyectos

Desde la interfaz web puedes:
- Crear proyectos de tipo **Image** o **Guard**
- Subir imágenes para conversión automática a pixel art
- Definir áreas de protección para el modo Guard
- Asignar bots específicos a cada proyecto

### 3. Monitorear actividad

El panel de control muestra:
- Estado de conexión de cada bot
- Progreso de proyectos activos
- Estadísticas de rendimiento
- Logs en tiempo real

---

## 🛠️ Uso

### Iniciar el sistema

```bash
# Método recomendado: usar el script de inicio
./start.sh

# Para desarrollo frontend únicamente
./start.sh --frontend-only

# Método manual con Docker Compose
docker-compose up -d
```

### Detener el sistema

```bash
# Detener todos los servicios
docker-compose down

# Detener y eliminar volúmenes (⚠️ elimina datos de BD)
docker-compose down -v
```

### Ver logs

```bash
# Logs de todos los servicios
docker-compose logs -f

# Logs de un servicio específico
docker-compose logs -f server  # FastAPI
docker-compose logs -f ui      # Interfaz web
docker-compose logs -f postgres # Base de datos
docker-compose logs -f redis   # Cache
```

---

## 🏗️ Desarrollo

### Ejecutar en modo desarrollo

```bash
# Backend (FastAPI)
cd server
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Frontend (Astro)
cd ui
npm install
npm run dev
```

### Estructura del proyecto

```
wplace-masterserver/
├── server/                 # Backend FastAPI
│   ├── main.py            # Aplicación principal
│   ├── requirements.txt   # Dependencias Python
│   └── Dockerfile         # Imagen Docker del servidor
├── ui/                    # Frontend Astro/React
│   ├── src/               # Código fuente
│   ├── package.json       # Dependencias Node.js
│   └── Dockerfile         # Imagen Docker de la UI
├── docker-compose.yml     # Orquestación de servicios
├── despliegue.sh         # Script de despliegue automatizado
└── README.md             # Este archivo
```

---

## 🤝 Proyectos relacionados

Este servidor está diseñado para trabajar con:

- **[WPlace AutoBOT](https://github.com/Alarisco/WPlace-AutoBOTV2-GuardBOT)**: Bots cliente que se conectan al master server <mcreference link="https://github.com/Alarisco/WPlace-AutoBOTV2-GuardBOT" index="1">1</mcreference>

---

## 🐛 Solución de problemas

### Error de conexión WebSocket

```bash
# Verificar que el servidor esté ejecutándose
docker-compose ps

# Ver logs del servidor
docker-compose logs server
```

### Problemas con certificados SSL

```bash
# Verificar permisos de certificados
ls -la /etc/letsencrypt/live/tu-dominio.com/

# Renovar certificados Let's Encrypt
sudo certbot renew
```

### Base de datos no inicializa

```bash
# Limpiar volúmenes y reiniciar
docker-compose down -v
docker-compose up -d
```

---

## 📄 Licencia

Este proyecto está bajo la Licencia MIT. Ver el archivo `LICENSE` para más detalles.

## ☕ Apoyo al desarrollo

Si este proyecto te ha sido útil, considera apoyar su desarrollo:

<p align="center">
  <a href="https://buymeacoffee.com/alarisco">
    <img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee">
  </a>
</p>

---

<p align="center">
  <strong>🎨 Hecho con ❤️ para la comunidad de WPlace – usa responsablemente</strong>
</p>