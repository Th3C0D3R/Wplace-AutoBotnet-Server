<#
.SYNOPSIS
  start.ps1 — Start/Update the WPlace Master Server locally
  Requirements: Docker Desktop, Docker Compose
  Usage: Run from this folder (wplace-masterserver/). Important: rebuilds and restarts services.
#>

param(
    [switch]$FrontendOnly,
    [switch]$NoRebuild,
    [switch]$Help
)

# Colors
function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[ERR ] $msg" -ForegroundColor Red }

if ($Help) {
    Write-Host "Uso: ./start.ps1 [OPCIONES]"
    Write-Host "Opciones:"
    Write-Host "  -FrontendOnly    Start/update only the frontend (UI service)"
    Write-Host "  -NoRebuild       Do not rebuild images, just restart services"
    Write-Host "  -Help            Show this help"
    exit 0
}

# Change directory to script location
Set-Location -Path $PSScriptRoot

if ($FrontendOnly) {
    Write-Info "Starting WPlace Frontend only..."
} else {
    Write-Info "Starting WPlace Master & Slave System..."
}
Write-Host "======================================"

# Verify Docker is running
Write-Info "Checking Docker..."
if (-not (docker info 2>$null)) {
    Write-Err "Docker isn't running. Please start Docker first."
    exit 1
}
Write-Ok "Docker is running"

# Detect docker compose
Write-Info "Detecting Docker Compose..."
if (docker compose version 2>$null) {
    $COMPOSE_CMD = "docker compose"
    Write-Ok "Usando 'docker compose' (plugin)"
} elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $COMPOSE_CMD = "docker-compose"
    Write-Ok "Using 'docker-compose' (standalone binary)"
} else {
    Write-Err "Docker Compose is unavailable. Please install and run it again."
    exit 1
}

# Create .env if missing
Write-Info "Checking .env configuration..."
if (-not (Test-Path ".env")) {
    Write-Info "Creating a basic .env file for WPlace Master Server..."
@"
# WPlace Master Server Configuration
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
"@ | Out-File -Encoding utf8 .env
    Write-Ok ".env file created"
} else {
    Write-Ok ".env file already exists"
}

# Handle NoRebuild (sync-only)
if ($NoRebuild) {
    Write-Info "Restarting services without rebuilding images..."
    if ($FrontendOnly) {
        Invoke-Expression "$COMPOSE_CMD restart ui"
        Write-Ok "Frontend restarted (no rebuild)"
    } else {
        Invoke-Expression "$COMPOSE_CMD restart"
        Write-Ok "All services restarted (no rebuild)"
    }
    exit 0
}

# Build & start services
if ($FrontendOnly) {
    Write-Info "Building and deploying front-end services..."
    Invoke-Expression "$COMPOSE_CMD stop ui"
    Invoke-Expression "$COMPOSE_CMD rm -f ui"

    if (-not (Invoke-Expression "$COMPOSE_CMD build --no-cache ui")) {
        Write-Err "Frontend build failed."
        exit 1
    }
    if (-not (Invoke-Expression "$COMPOSE_CMD up -d ui")) {
        Write-Err "Failed to start frontend container."
        exit 1
    }
} else {
    Write-Info "Building and erecting all services..."
    if (-not (Invoke-Expression "$COMPOSE_CMD build --no-cache server ui")) {
        Write-Err "The construction failed."
        exit 1
    }
    if (-not (Invoke-Expression "$COMPOSE_CMD up -d server ui redis postgres")) {
        Write-Err "Failed to start containers."
        exit 1
    }
}

# Wait for services
Write-Info "Waiting for services to start..."
Start-Sleep -Seconds 10

# Health checks
Write-Info "Checking the health of the services..."
if ($FrontendOnly) {
    try {
        Invoke-WebRequest http://localhost:3004 -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Ok "Astro frontend running on http://localhost:3004"
    } catch { Write-Warn "Frontend Astro is not responding" }
} else {
    try {
        Invoke-WebRequest http://localhost:8008/health -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Ok "FastAPI server running on http://localhost:8008"
    } catch { Write-Warn "FastAPI server is not responding" }

    try {
        Invoke-WebRequest http://localhost:3004 -UseBasicParsing -TimeoutSec 5 | Out-Null
        Write-Ok "Astro frontend running on http://localhost:3004"
    } catch { Write-Warn "Frontend Astro is not responding" }

    if (Invoke-Expression "$COMPOSE_CMD exec -T redis redis-cli ping" 2>$null) {
        Write-Ok "Redis is running"
    } else {
        Write-Warn "Redis is not responding"
    }

    if (Invoke-Expression "$COMPOSE_CMD exec -T postgres pg_isready -U wplace" 2>$null) {
        Write-Ok "PostgreSQL is running"
    } else {
        Write-Warn "PostgreSQL is not responding"
    }
}

Write-Host ""
if ($FrontendOnly) {
    Write-Ok "WPlace Frontend is ready!"
    Write-Host "📊 Dashboard: http://localhost:3004"
} else {
    Write-Ok "WPlace Master System is ready!"
    Write-Host "📊 Dashboard: http://localhost:3004"
    Write-Host "🔧 API Docs:  http://localhost:8008/docs"
    Write-Host "📝 API Health: http://localhost:8008/health"
}
