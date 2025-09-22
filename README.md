# ⚠️ Beta Version – May contain bugs

> **IMPORTANT**: This project is currently in **beta phase**.
> Some features may not be fully implemented or may be buggy.
> Please use with caution and report any issues.

---

# WPlace Master Server

<p align="center"> 
<strong>🎯 Master server to coordinate WPlace bots with management web interface</strong>
</p>

<p align="center"> 
<img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI"> 
<img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker"> 
<img src="https://img.shields.io/badge/Astro-FF5D01?style=for-the-badge&logo=astro&logoColor=white" alt="Astro"> 
<img src="https://img.shields.io/badge/PostgreSQL-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL"> 
<img src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white" alt="Redis">
</p>

## ☕ Development support

If this project has been useful to you, consider supporting its development:

<p align="center"> 
  <a href="https://buymeacoffee.com/alarisco"> 
    <img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee">
  </a>
</p>

---
## 📋 Description

WPlace Master Server is a centralized coordination system that allows you to efficiently manage multiple WPlace bots. It provides a modern web interface for controlling slave bots, configuring protection (Guard) and automatic painting (Image) projects, and monitoring their status in real time.

### ✨ Key Features

- 🤖 **Slave Bot Management**: Connect and coordinate multiple bot instances
- 🎨 **Image Mode**: Automation for creating pixel art from images
- 🛡️ **Guard Mode**: Automatic protection of specific areas of the canvas
- 🌐 **Modern Web Interface**: Intuitive dashboard built with Astro and React
- 📊 **Real-Time Monitoring**: WebSockets for instant updates
- 🔄 **Smart Compression**: Automatic optimization of large messages
- 📈 **Advanced Telemetry**: Detailed performance statistics

---

## 🚀 Quick Start

### Method 1: Autostart Script (Recommended)

The project includes a <mcfile name="start.sh" script path="/Users/alvaroalonso/workspace/project-place/wplace-masterserver/start.sh"></mcfile> which simplifies the startup process:

(LINUX)

```bash
# Make the script executable
chmod +x start.sh

# Start the entire system
./start.sh

# Start only the frontend (useful for development)
./start.sh --frontend-only

# View help
./start.sh --help
```

(WINDOWS)

```ps1
# Start the entire system
powershell.exe .\start.ps1

# Start only the frontend (useful for development)
powershell.exe .\start.ps1 -FrontendOnly

# View help
powershell.exe .\start.ps1 -Help
```
 
**Script Features:**
- ✅ Automatic Docker check
- ✅ Automatic creation of the `.env` file
- ✅ Building and starting containers
- ✅ Service health check
- ✅ Frontend-only mode for development
- ✅ Informational messages and troubleshooting

### Method 2: Docker Compose Manually

If you prefer manual control over the process:

### Prerequisites

- 🐳 **Docker** and **Docker Compose** installed
- 🌐 **Port 3004** (web interface) and **8008** (API) available
- 💾 At least **2GB of RAM** available for containers

### Installation

```bash
# 1. Clone the repository
git clone <repository-url>
cd wplace-masterserver

# 2.1. Use the startup script (recommended)
chmod +x start.sh
./start.sh

# 2.2. Use the startup script (recommended)
powershell.exe .\\start.ps1
```

### 2. Local deployment (development)

```bash
# Create configuration file
cat > .env << EOF
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
EOF

# Build and start services
docker-compose up -d --build

# View logs in real time
docker-compose logs -f

# Verify that everything works
docker-compose ps
```

**Access:**
- 🌐 **Web interface**: http://localhost:3004
- 🔧 **Server API**: http://localhost:8008
- 📊 **PostgreSQL database**: localhost:5432 (user: `wplace`, password: `wplace123`, db: `wplace_master`)
- 🗄️ **Redis**: localhost:6379

### 3. Production Deployment

For deployments to remote servers, use the automated script:

(LINUX)

```bash
# Make the script executable
chmod +x deployment.sh

# Run deployment
./deployment.sh
```

(WINDOWS)

```ps1
# Run deployment
./deployment.ps1
```

The script will prompt you for:
- Server IP or domain
- SSH username
- SSH password
- Installation path (default: `/opt/wplace-masterserver`)

---

## 🔒 SSL Configuration (Important for non-localhost bots)

> ⚠️ **WARNING**: If you plan to use bots other than localhost, you MUST configure valid SSL certificates. Modern browsers block insecure WebSocket connections from HTTPS sites.

### Option 1: Nginx Proxy Manager (Recommended)

**Nginx Proxy Manager** is the easiest way to manage SSL certificates automatically:

1. **Install Nginx Proxy Manager:**
```bash
# Create directory for NPM
mkdir nginx-proxy-manager
cd nginx-proxy-manager

# Download docker-compose.yml from NPM
curl -o docker-compose.yml https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/main/docker-compose.yml

# Start NPM
docker-compose up -d
```

2. **Configure the proxy:**
- Go to `http://your-server:81`
- Initial login: `admin@example.com` / `changeme`
- Create a new "Proxy Host":
- **Domain**: `your-domain.com`
- **Forward Hostname/IP**: `your-server-ip`
- **Forward Port**: `8008` (WPlace Master Server port)
- In the "SSL":
- Check "Request a new SSL Certificate"
- Check "Force SSL"
- Accept the Let's Encrypt terms

3. **NPM Advantages:**
- ✅ Automatic certificate renewal
- ✅ Intuitive web interface
- ✅ Support for multiple domains
- ✅ Automatic reverse proxy configuration

### Option 2: Let's Encrypt manually (LINUX ONLY FOR NOW)

If you prefer to configure Let's Encrypt manually:

```bash
# Install Certbot
sudo apt install certbot # Ubuntu/Debian
# brew install certbot # macOS

# Generate certificates
sudo certbot certonly --standalone -d your-domain.com

# Copy certificates
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ./certs/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem ./certs/
sudo chown $USER:$USER ./certs/*
```

### Option 3: Self-signed certificates (Development only)

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/privkey.pem -out certs/fullchain.pem -days 365 -nodes -subj "/C=ES/ST=State/L=City/O=Organization/CN=localhost"
```

### Configuration in Docker Compose

To use local certificates, uncomment the SSL lines in `docker-compose.yml`:

```yaml
server:
volumes:
- ./certs:/app/certs # Uncomment this line
environment:
- SSL_CERT_PATH=/app/certs/fullchain.pem # Uncomment 
- SSL_KEY_PATH=/app/certs/privkey.pem # Uncomment
```

---

## 🏗️ System architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Interface Web  │    │  Master Server  │    │   Bot Esclavo   │
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


### Core Components

- **Master Server**: FastAPI with WebSockets for coordination
- **Web Interface**: Control panel built with Astro and React
- **PostgreSQL**: Database for configuration persistence
- **Redis**: Real-time session caching and management

---

## 🔧 Advanced Configuration

### Remote Server Configuration

By default, the web interface connects to the local server (`localhost:8008`). To connect to a remote server, edit the `docker-compose.yml` file directly:

#### Configuration in docker-compose.yml

1. **Open the `docker-compose.yml` file**
2. **Locate the `ui` service** and the `build` > `args` section
3. **Modify the `SERVER_URL` line:**

```yaml
ui:
build:
context: ./ui
dockerfile: Dockerfile
args:
# Change this line to configure the remote server
- SERVER_URL="http://your-server:8008" # ← Edit here
# ... rest of the configuration
```

**Configuration examples:**

```yaml
# Local server (default)
- SERVER_URL=""

# Server on the local network
- SERVER_URL="http://192.168.1.100:8008"

# Remote server with domain
- SERVER_URL="https://wplace.my-domain.com:8008"

# Docker server with specific IP
- SERVER_URL="http://10.0.0.5:8008"
```

#### Apply changes

After modifying `docker-compose.yml`:

```bash
# Rebuild only the UI service to apply changes
docker-compose up -d --build ui

# Or rebuild the entire service if you prefer
docker-compose up -d --build
```

> **💡 Tip**: If you're using HTTPS for the server, make sure you have valid SSL certificates configured to avoid WebSocket connection issues.

### Environment Variables

Create a `.env` file in the project root:

```env
# Database
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master

# Redis
REDIS_URL=redis://redis:6379

# SSL (production only)
SSL_CERT_PATH=/app/certs/fullchain.pem
SSL_KEY_PATH=/app/certs/privkey.pem

# Server Configuration
PYTHONUNBUFFERED=1
```

> **Note**: The remote server configuration (`SERVER_URL`) is done directly in `docker-compose.yml`, not via environment variables.

### Port Customization

Modify the <mcfile name="docker-compose.yml" path="/Users/alvaroalonso/workspace/project-place/wplace-masterserver/docker-compose.yml"></mcfile> to suit your needs:

```yaml
services:
server:
ports:
- "8008:8000" # Current server port
ui:
ports:
- "3004:3000" # Current web interface port
postgres:
ports:
- "5432:5432" # PostgreSQL port
redis:
ports:
- "6379:6379" # Redis port
```

---

## 📖 Using the System

### 1. Connecting Slave Bots

Bots automatically connect to the master server via WebSocket:

```
ws://your-server:8008/ws/slave/{slave_id}
```

### 2. Configure Projects

From the web interface you can:
- Create **Image** or **Guard** type projects
- Upload images for automatic conversion to pixel art
- Define protection areas for Guard mode
- Assign specific bots to each project

### 3. Monitor Activity

The dashboard displays:
- Connection status of each bot
- Progress of active projects
- Performance statistics
- Real-time logs

---

## 🛠️ Usage

### Start the system

```bash
# Recommended method: Use the startup script
./start.sh    #OR    powershell.exe .\start.ps1

# For frontend development only
./start.sh --frontend-only    #OR    powershell.exe .\start.ps1 -FrontendOnly

# Manual method with Docker Compose
docker-compose up -d
```

### Stop the system

```bash
# Stop all services
docker-compose down

# Stop and delete volumes (⚠️ deletes database data)
docker-compose down -v
```

### View logs

```bash
# Logs for all services
docker-compose logs -f

# Logs for a specific service
docker-compose logs -f server # FastAPI
docker-compose logs -f ui # Web interface
docker-compose logs -f postgres # Database
docker-compose logs -f redis # Cache
```

---

## 🏗️ Development

### Run in development mode

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

### Project structure

```
wplace-masterserver/
├── server/ # Backend FastAPI
│ ├── main.py # Main application
│ ├── requirements.txt # Python dependencies
│ └── Dockerfile # Server Docker image
├── ui/ # Astro/React frontend
│ ├── src/ # Source code
│ ├── package.json # Node.js dependencies
│ └── Dockerfile # UI Docker image
├── docker-compose.yml # Service orchestration
├── deployment.sh # Automated deployment script
└── README.md # This file
```

---

## 🤝 Projects Related

This server is designed to work with:

- **[WPlace AutoBOT](https://github.com/Alarisco/WPlace-AutoBOTV2-GuardBOT)**: Client bots connecting to the master server <mcreference link="https://github.com/Alarisco/WPlace-AutoBOTV2-GuardBOT" index="1">1</mcreference>

---

## 🐛 Troubleshooting

### WebSocket connection error

```bash
# Verify that the server is running
docker-compose ps

# View server logs
docker-compose logs server
```

### SSL certificate problems

```bash
# Verify certificate permissions
ls -la /etc/letsencrypt/live/your-domain.com/

# Renew Let's Encrypt certificates
sudo certbot renew
```

### Base Data not initialized

```bash
# Clean volumes and reboot
docker-compose down -v
docker-compose up -d
```

---

## 📄 License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## ☕ Development Support

If you found this project helpful, please consider supporting its development:

<p align="center">
<a href="https://buymeacoffee.com/alarisco">
<img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee">
</a>
</p>

---

<p align="center">
<strong>🎨 Made with ❤️ for the WPlace community – use responsibly</strong>
</p>