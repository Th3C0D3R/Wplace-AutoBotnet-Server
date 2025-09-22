# deploy.ps1 — Deploys/updates the WPlace Master Server on a remote host
# Requirements: ssh, rsync (optional), sshpass (optional for non-interactive password)
# Usage: Run from this folder (wplace-masterserver/). Important: syncs and rebuilds the service.

param(
    [switch]$SyncOnly,
    [switch]$Help
)

if ($Help) {
    Write-Host "Usage: .\deploy.ps1 [-SyncOnly] -> Sync files without restarting/rebuilding containers"
    exit 0
}

# Colors
function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Blue }
function Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "[ERR ] $msg" -ForegroundColor Red }
function Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }

# Interactive questions
$ServerIP = Read-Host "Server IP or domain (e.g., 192.168.1.19)"
if ([string]::IsNullOrWhiteSpace($ServerIP)) { $ServerIP = "192.168.1.19" }

$SSHUser = Read-Host "SSH User [root]"
if ([string]::IsNullOrWhiteSpace($SSHUser)) { $SSHUser = "root" }

$SSHPassword = Read-Host -AsSecureString "SSH Password (will be used if sshpass available, otherwise it will prompt)"
$RemoteDir = Read-Host "Remote destination path [/opt/wplace-masterserver]"
if ([string]::IsNullOrWhiteSpace($RemoteDir)) { $RemoteDir = "/opt/wplace-masterserver" }

$Remote = "$SSHUser@$ServerIP"

# Detect tools
$HasSshpass = if (Get-Command sshpass -ErrorAction SilentlyContinue) { 1 } else { 0 }
if (-not (Get-Command rsync -ErrorAction SilentlyContinue)) { Warn "rsync not installed locally; will fallback to tar/ssh if needed" }
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Err "ssh not installed locally"; exit 1 }

# Helper to run remote command
function Run-Remote($cmd) {
    if ($HasSshpass -eq 1) {
        $plainPass = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($SSHPassword))
        sshpass -p $plainPass ssh -o StrictHostKeyChecking=no $Remote $cmd
    } else {
        ssh -o StrictHostKeyChecking=no $Remote $cmd
    }
}

# Create remote directory
Info "Creating remote directory $RemoteDir..."
Run-Remote "mkdir -p $RemoteDir"

# Sync code
$Excludes = @(
    "--exclude .git/"
    "--exclude __pycache__/"
    "--exclude .DS_Store"
    "--exclude .venv/"
    "--exclude node_modules/"
    "--exclude *.log"
)

$RemoteHasRsync = Run-Remote "command -v rsync >/dev/null 2>&1; echo $?" 
$DoRsync = 0
if ((Get-Command rsync -ErrorAction SilentlyContinue) -and ($RemoteHasRsync -eq 0)) { $DoRsync = 1 }

if ($DoRsync -eq 1) {
    if ($HasSshpass -eq 1) {
        $plainPass = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($SSHPassword))
        Info "Syncing (rsync) -> $Remote:$RemoteDir ..."
        sshpass -p $plainPass rsync -az --delete -e "ssh -o StrictHostKeyChecking=no" $Excludes ./ "$Remote:$RemoteDir/"
    } else {
        Info "Syncing (rsync) -> $Remote:$RemoteDir ..."
        rsync -az --delete -e "ssh -o StrictHostKeyChecking=no" $Excludes ./ "$Remote:$RemoteDir/"
    }
} else {
    Warn "rsync not available on remote/local; using tar/ssh fallback"
    tar czf - --exclude .git --exclude __pycache__ --exclude .DS_Store --exclude .venv --exclude node_modules --exclude '*.log' . | ssh -o StrictHostKeyChecking=no $Remote "mkdir -p $RemoteDir && tar xzf - -C $RemoteDir"
}

Ok "Code synchronized"

if ($SyncOnly) {
    Ok "Sync completed (sync-only). Containers not restarted/rebuilt, .env untouched."
    exit 0
}

# Ensure remote .env exists
Info "Checking remote .env configuration"
if (-not (Run-Remote "test -f $RemoteDir/.env && echo exists")) {
    Info "Creating basic .env for WPlace Master Server"
    Run-Remote @"
cat > $RemoteDir/.env <<'EOF_ENV'
# WPlace Master Server Configuration
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
EOF_ENV
"@
    Ok ".env created"
} else {
    Ok ".env already exists, leaving untouched"
}

# Check Docker
Info "Checking Docker on remote"
if (-not (Run-Remote "docker --version >/dev/null 2>&1 && echo ok")) {
    Err "Docker is not installed on the server. Install it and re-run this script."
    exit 1
}

$ComposeCmd = "docker compose"
if (-not (Run-Remote "docker compose version >/dev/null 2>&1 && echo ok")) {
    Warn "Docker Compose plugin not detected. Trying 'docker-compose'"
    if (-not (Run-Remote "docker-compose version >/dev/null 2>&1 && echo ok")) {
        Err "No docker compose available (neither plugin nor binary). Install it and re-run."
        exit 1
    }
    $ComposeCmd = "docker-compose"
}

# Build and start services
Info "Building and starting services on remote..."
Run-Remote "cd $RemoteDir && $ComposeCmd up -d --build"
Ok "Deployment completed"

# Health check
Info "Checking health..."
if (Run-Remote "curl -fsS http://localhost:8008/health >/dev/null && echo healthy") {
    Ok "Master Server API healthy at http://$ServerIP:8008/"
    Ok "Dashboard UI available at http://$ServerIP:3004/"
} else {
    Warn "Health check failed. Check logs with: cd $RemoteDir && $ComposeCmd logs -f server"
}

# Final tips
@"
🎉 WPlace Master Server successfully deployed!
====================================================

📊 Services available:
- Dashboard UI: http://$ServerIP:3004
- API Server:   http://$ServerIP:8008
- API Docs:     http://$ServerIP:8008/docs
- Health Check: http://$ServerIP:8008/health

📋 Next steps:
- Configure your firewall to allow ports 3004 and 8008
- For public access, set up a reverse proxy (Nginx/Traefik)
- To connect slaves, use the URL: ws://$ServerIP:8008/ws/slave

🔧 Useful commands:
- Server logs: ssh $SSHUser@$ServerIP "cd $RemoteDir && $ComposeCmd logs -f server"
- UI logs:     ssh $SSHUser@$ServerIP "cd $RemoteDir && $ComposeCmd logs -f ui"
- All logs:    ssh $SSHUser@$ServerIP "cd $RemoteDir && $ComposeCmd logs -f"
- Restart:     ssh $SSHUser@$ServerIP "cd $RemoteDir && $ComposeCmd restart"
- Update:      re-run this script locally

📚 Additional documentation: README.md and USAGE.md
"@
