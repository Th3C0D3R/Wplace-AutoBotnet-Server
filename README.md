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

## Compression of Large WebSocket Messages

When a JSON message (preview_data, guardData, etc.) exceeds 20MB, the server or slave automatically wraps and compresses it using gzip + base64 to avoid frame size limits.

Wrapper format:
```json
{
	"type": "__compressed__",
	"encoding": "gzip+base64",
	"originalType": "preview_data",        // original message type
	"originalLength": 24567890,             // bytes of original JSON UTF-8
	"compressedLength": 1234567,            // length of base64 payload string
	"payload": "H4sIAAAAA..."              // base64(gzip(JSON))
}
```

The receiver transparently decompresses and processes the original object. This is fully backward-compatible: smaller messages remain unwrapped.

Threshold can be tuned in code (`COMPRESSION_THRESHOLD`).

## API Endpoints

- `GET /api/slaves` - List connected slaves
- `POST /api/projects` - Create new project
- `WS /ws/slave` - Slave WebSocket connection
- `WS /ws/ui` - UI real-time updates