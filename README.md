# WPlace Maestro & Slave System

Centralized management system for multiple WPlace AutoBOT instances.

## Architecture

### Slave (Browser Client)
- New entrypoint: `Auto-Slave.js`
- WebSocket connection to Master server
- Periodic telemetry transmission
- Execution of Image, Guard, or Farm bots

### Master (Server)
- **FastAPI (Python)** → REST API + WebSocket
- **Astro (Frontend)** → Modern and minimalist UI
- **Redis** → Queues and state (future)
- **Postgres** → Project/session persistence (future)
- **Docker Compose** → Local/server deployment

## Project Structure

```
wplace-masterserver/
├── server/                # FastAPI backend
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── ui/                    # Astro frontend
│   ├── src/
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml     # Local orchestration
└── README.md             # This document
```

## Quick Start

1. Clone the repository
2. Run `docker-compose up`
3. Access the dashboard at `http://localhost:3000`
4. Inject `Auto-Slave.js` in your browser on wplace.live

## API Endpoints

- `GET /api/slaves` - List connected slaves
- `POST /api/projects` - Create new project
- `WS /ws/slave` - Slave WebSocket connection
- `WS /ws/ui` - UI real-time updates