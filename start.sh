#!/bin/bash

# WPlace Master Server Startup Script

set -e

# Parse command line arguments
FRONTEND_ONLY=false
SERVICES="all"

while [[ $# -gt 0 ]]; do
    case $1 in
        --frontend-only)
            FRONTEND_ONLY=true
            SERVICES="ui"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --frontend-only    Deploy/update only the frontend (ui service)"
            echo "  --help, -h         Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                 # Deploy all services (default)"
            echo "  $0 --frontend-only # Deploy only the frontend"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

if [ "$FRONTEND_ONLY" = true ]; then
    echo "🎨 Starting WPlace Frontend Only..."
else
    echo "🚀 Starting WPlace Master & Slave System..."
fi
echo "======================================"

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker first."
    exit 1
fi

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose not found. Please install Docker Compose."
    exit 1
fi

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env file..."
    cat > .env << EOF
# WPlace Master Server Configuration
REDIS_URL=redis://redis:6379
DATABASE_URL=postgresql://wplace:wplace123@postgres:5432/wplace_master
PYTHONUNBUFFERED=1
EOF
fi

# Build and start services
if [ "$FRONTEND_ONLY" = true ]; then
    echo "🔨 Building and starting frontend service..."
    echo "📋 Stopping existing frontend container..."
    docker-compose stop ui || true
    docker-compose rm -f ui || true
    
    echo "🏗️ Building frontend image..."
    if ! docker-compose build --no-cache ui; then
        echo "❌ Frontend build failed. Check the logs above for details."
        echo "💡 Common solutions:"
        echo "   - Ensure Docker has enough memory (4GB+ recommended)"
        echo "   - Check internet connection for package downloads"
        echo "   - Try: docker system prune -f to clean up space"
        exit 1
    fi
    
    echo "🚀 Starting frontend container..."
    if ! docker-compose up -d ui; then
        echo "❌ Failed to start frontend container. Checking logs..."
        docker-compose logs ui
        exit 1
    fi
else
    echo "🔨 Building and starting all services (without stopping database)..."
    # IMPORTANT: Avoid bringing the whole stack down to prevent Postgres fast shutdowns and WS 1012 disconnects
    # Build only app images; redis/postgres use official images and don't require build
    echo "🏗️ Building images (server and ui, this may take a few minutes)..."
    if ! docker-compose build --no-cache server ui; then
        echo "❌ Build failed. Check the logs above for details."
        echo "💡 Common solutions:"
        echo "   - Ensure Docker has enough memory (4GB+ recommended)"
        echo "   - Check internet connection for package downloads"
        echo "   - Try: docker system prune -f to clean up space"
        exit 1
    fi
    
    echo "🚀 Starting/Updating containers..."
    # Up only recreates services that changed; Postgres remains running if unchanged
    if ! docker-compose up -d server ui redis postgres; then
        echo "❌ Failed to start containers. Checking logs..."
        docker-compose logs
        exit 1
    fi
fi

# Wait for services to be ready
echo "⏳ Waiting for services to start..."
sleep 10

# Check service health
echo "🔍 Checking service health..."

if [ "$FRONTEND_ONLY" = true ]; then
    # Check only Astro frontend
    if curl -f http://localhost:3004 > /dev/null 2>&1; then
        echo "✅ Astro frontend is running at http://localhost:3004"
    else
        echo "❌ Astro frontend is not responding"
    fi
else
    # Check FastAPI server
    if curl -f http://localhost:8008/health > /dev/null 2>&1; then
        echo "✅ FastAPI server is running at http://localhost:8008"
    else
        echo "❌ FastAPI server is not responding"
    fi
    
    # Check Astro frontend
    if curl -f http://localhost:3004 > /dev/null 2>&1; then
        echo "✅ Astro frontend is running at http://localhost:3004"
    else
        echo "❌ Astro frontend is not responding"
    fi
    
    # Check Redis
    if docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; then
        echo "✅ Redis is running"
    else
        echo "❌ Redis is not responding"
    fi
    
    # Check PostgreSQL
    if docker-compose exec -T postgres pg_isready -U wplace > /dev/null 2>&1; then
        echo "✅ PostgreSQL is running"
    else
        echo "❌ PostgreSQL is not responding"
    fi
fi

echo ""
if [ "$FRONTEND_ONLY" = true ]; then
    echo "🎉 WPlace Frontend is ready!"
    echo "======================================"
    echo "📊 Dashboard: http://localhost:3004"
    echo ""
    echo "📋 Next steps:"
    echo "1. Open your browser and go to http://localhost:3004"
    echo "2. Navigate to https://wplace.live in another tab"
    echo "3. Inject the Auto-Slave.js script using one of these methods:"
    echo "   - Browser extension (recommended)"
    echo "   - Bookmarklet injection"
    echo "   - Manual script injection in console"
    echo ""
    echo "📜 View frontend logs with: docker-compose logs -f ui"
    echo "🛑 Stop frontend with: docker-compose stop ui"
else
    echo "🎉 WPlace Master System is ready!"
    echo "======================================"
    echo "📊 Dashboard: http://localhost:3004"
    echo "🔧 API Docs:  http://localhost:8008/docs"
    echo "📝 API Health: http://localhost:8008/health"
    echo ""
    echo "📋 Next steps:"
    echo "1. Open your browser and go to http://localhost:3004"
    echo "2. Navigate to https://wplace.live in another tab"
    echo "3. Inject the Auto-Slave.js script using one of these methods:"
    echo "   - Browser extension (recommended)"
    echo "   - Bookmarklet injection"
    echo "   - Manual script injection in console"
    echo ""
    echo "📜 View logs with: docker-compose logs -f"
    echo "🛑 Stop system with: docker-compose down"
fi
echo ""