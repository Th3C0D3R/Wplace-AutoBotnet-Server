# WPlace Master & Slave System - Usage Guide

## 🚀 Quick Start

### 1. Start the Master Server

```bash
cd wplace-masterserver
./start.sh
```

This will:
- Build and start all Docker containers
- Initialize the PostgreSQL database
- Start Redis for caching
- Launch the FastAPI backend on port 8008
- Launch the Astro frontend on port 3000

### 2. Access the Dashboard

Open your browser and navigate to: **http://localhost:3000**

You'll see the WPlace Master Dashboard with:
- Connected Slaves panel (initially empty)
- Control Panel for managing bots
- Real-time telemetry display
- Activity logs

### 3. Connect Slaves

To connect browser instances as slaves:

#### Option A: Browser Extension (Recommended)
1. Install the WPlace AutoBOT Chrome extension
2. Navigate to https://wplace.live
3. Click the extension icon and select "Slave Mode"

#### Option B: Bookmarklet
1. Create a new bookmark with this JavaScript code:
```javascript
javascript:(async()=>{const U="https://raw.githubusercontent.com/Alarisco/WPlace-AutoBOT/refs/heads/main/Auto-Slave.js";try{const r=await fetch(U,{cache:"no-cache"});if(!r.ok)throw new Error(r.status+" "+r.statusText);const code=await r.text();const blob=new Blob([code+"\n//# sourceURL="+U],{type:"text/javascript"});const blobUrl=URL.createObjectURL(blob);try{await new Promise((ok,err)=>{const s=document.createElement("script");s.src=blobUrl;s.onload=ok;s.onerror=err;document.documentElement.appendChild(s);});}catch(e){await import(blobUrl);}}catch(e){alert("[Auto-Slave] Could not load/inject: "+e.message+"\nTry another page or use Option C (module).");})();
```
2. Navigate to https://wplace.live
3. Click the bookmarklet

#### Option C: Manual Console Injection
1. Navigate to https://wplace.live
2. Open Developer Tools (F12)
3. Go to Console tab
4. Paste and execute the Auto-Slave.js code

### 4. Configure and Start a Session

1. **Select Connected Slaves**: Check the boxes next to the slaves you want to use
2. **Choose Bot Mode**: Mode will be auto-detected from uploaded project file
3. **Upload Project File** (optional): For Image/Guard modes, upload a JSON configuration file
4. **Select Charge Strategy**: Choose how to distribute pixel placement charges
5. **Click Start Session**: Begin coordinated bot operation

## 📊 Understanding the Dashboard

### Connected Slaves Panel
- Shows all browser instances connected as slaves
- Displays slave ID, status, and connection indicator
- Use checkboxes to select which slaves to include in sessions

### Control Panel
- **Bot Mode**: Auto-detected from project file - Image (auto-paint) or Guard (protect/repair)
- **Project File**: Upload JSON configurations for Image/Guard bots
- **Charge Strategy**: 
  - **Balanced**: Distribute charges evenly across all slaves
  - **Drain One Slave**: Use up one slave's charges before moving to the next
  - **Priority Based**: Use slaves in order of selection

### Real-time Telemetry
- **Repaired Pixels**: Total pixels placed/repaired across all slaves
- **Missing Pixels**: Pixels that need attention
- **Absent Pixels**: Pixels completely missing from protected areas
- **Remaining Charges**: Total charges available across all slaves

### Activity Logs
- Real-time log stream from all connected slaves
- Shows connection events, bot actions, and errors
- Automatically scrolls to show latest activity

## 🔧 Bot Modes Explained

### Image Mode
- **Purpose**: Automatically paint pixel art from uploaded images
- **Configuration**: Upload a JSON file with image URL and coordinates
- **Use Case**: Collaborative art creation with multiple accounts

### Guard Mode
- **Purpose**: Protect and repair existing pixel art
- **Configuration**: Define protected areas via JSON coordinates
- **Use Case**: Defend community art from vandalism



## 📁 Project Configuration Files

### Image Project JSON Example
```json
{
  "image_url": "https://example.com/pixel-art.png",
  "start_x": 100,
  "start_y": 100,
  "scale": 1,
  "dithering": false
}
```

### Guard Project JSON Example
```json
{
  "protected_areas": [
    {
      "name": "Logo Area",
      "x1": 100,
      "y1": 100,
      "x2": 200,
      "y2": 200,
      "priority": "high"
    }
  ],
  "repair_threshold": 5
}
```



## 🔍 Monitoring and Troubleshooting

### Check System Status
```bash
# View all service logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f server
docker-compose logs -f ui

# Check service health
curl http://localhost:8008/health
```

### Common Issues

**Slaves not connecting:**
- Ensure the Master server is running on localhost:8008
- Check browser console for WebSocket connection errors
- Verify firewall settings allow connections to port 8008

**Dashboard not loading:**
- Confirm Astro frontend is running on port 3000
- Check Docker container status: `docker-compose ps`
- Review nginx logs: `docker-compose logs ui`

**Database errors:**
- Restart PostgreSQL: `docker-compose restart postgres`
- Check database connectivity: `docker-compose exec postgres pg_isready -U wplace`

### Performance Tips

1. **Optimal Slave Count**: 3-5 slaves per session for best coordination
2. **Charge Management**: Use "Balanced" strategy for consistent progress
3. **Network Stability**: Ensure stable internet connection for all slave browsers
4. **Resource Usage**: Monitor CPU/memory usage with many concurrent slaves

## 🛑 Stopping the System

```bash
# Stop all services
docker-compose down

# Stop and remove all data (reset)
docker-compose down -v
```

## 🔒 Security Considerations

- The system is designed for local development/testing
- For production use, implement proper authentication
- Use HTTPS and WSS in production environments
- Consider rate limiting and abuse prevention

## 📈 Advanced Usage

### API Integration
The FastAPI backend provides REST endpoints for programmatic control:

```bash
# Get connected slaves
curl http://localhost:8008/api/slaves

# Create a project
curl -X POST http://localhost:8008/api/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Project", "mode": "Image", "config": {}}'

# Start a session
curl -X POST http://localhost:8008/api/sessions/{session_id}/start
```

### WebSocket Integration
Connect directly to WebSocket endpoints for real-time updates:

```javascript
// UI updates
const uiWs = new WebSocket('ws://localhost:8008/ws/ui');

// Slave connection
const slaveWs = new WebSocket('ws://localhost:8008/ws/slave');
```

This completes the basic usage guide. For more advanced features and customization, refer to the API documentation at http://localhost:8008/docs when the server is running.