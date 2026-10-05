# This file is: ./web_server.py

"""
Video Library Manager - Main Application
==========================================
Streamlined web server that coordinates all route modules.

Original file was 2,215 lines - now reduced to ~250 lines.
All route logic has been moved to focused modules in routes/ folder.
"""

# Import logger FIRST to set up timestamped print()
try:
    import logger  # This sets up logging and replaces print()
    from logger import log_info, log_error
    log_info("Logger initialized")
except Exception as e:
    print(f"Warning: Could not initialize logger: {e}")
    def log_info(msg): print(msg)
    def log_error(msg): print(msg)

import os
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import shutil
import glob
from datetime import datetime

# Import configuration
from config import (
    CACHE_DIR, WATCH_HISTORY_DIR, SHOW_INACTIVITY_TIMER, 
    INACTIVITY_TIMEOUT, USE_SQLITE
)

# Import your existing business logic modules
from model import LibraryModel, UserModel
from media_handler import MediaHandler
from stream_handler import stream_manager
from watch_history import WatchHistory
from search_parser import SearchParser
from cast_manager import CastManager

# Import helper utilities
from helpers import check_auth, load_json_file

# Import all route modules
from routes import (
    auth_router,
    streaming_router,
    watch_history_router,
    cast_router,
    ui_router,
    ui_api_router,
    library_router,
    metadata_router,
    scanning_router,
    thumbnails_router,
    videos_router,
    files_router,
    playlists_router,
    direct_play_router,
)

# Import cast attributes and tools routers separately (not in routes package yet)
from ext_db import router as ext_db_router
from tools_api import router as tools_router
# New player (roadmap #8) - separate from the classic streaming routes
from routes.player import router as player_router
# Home / Recommended rows (roadmap #9)
from routes.home import router as home_router
from routes.collections import router as collections_router


# --- BACKUP FUNCTION ---

def backup_database():
    """Copy the database to <data>/backups on every start; keeps the newest STARTUP_BACKUPS (0 = off)."""
    if not USE_SQLITE:
        return
    
    from config import DATA_DIR as data_dir
    db_path = os.path.join(data_dir, 'library.db')
    
    from config import STARTUP_BACKUPS
    if not os.path.exists(db_path) or STARTUP_BACKUPS <= 0:
        return
    
    backup_dir = os.path.join(data_dir, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f'library_{timestamp}.db')
    
    try:
        shutil.copy2(db_path, backup_path)
        print(f"Database backed up: {os.path.basename(backup_path)}")
        
        # Keep the newest STARTUP_BACKUPS
        backups = sorted(glob.glob(os.path.join(backup_dir, 'library_*.db')))
        for old in backups[:-STARTUP_BACKUPS]:
            os.remove(old)
    except Exception as e:
        print(f"Backup failed: {e}")

# --- APPLICATION SETUP ---

app = FastAPI(title="Video Library Manager")

# Initialize your existing models
model = LibraryModel()
user_model = UserModel()
watch_history_instance = WatchHistory(WATCH_HISTORY_DIR)

# --- STATIC FILE SERVING ---

# Ensure cache directory exists
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)
app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")

# Static assets (themes, etc.)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


@app.get("/static/themes/{name}.css")
def theme_css(name: str):
    """Built-in themes ship in static/themes; uploaded ones live in <data>/themes."""
    import re as _re
    from fastapi.responses import FileResponse
    from config import DATA_DIR
    if not _re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        return Response(status_code=404)
    for folder in (os.path.join(STATIC_DIR, 'themes'), os.path.join(DATA_DIR, 'themes')):
        path = os.path.join(folder, name + '.css')
        if os.path.isfile(path):
            return FileResponse(path, media_type='text/css')
    return Response(status_code=404)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Media file serving
if not os.path.exists("/media"):
    os.makedirs("/media", exist_ok=True)
#app.mount("/files", StaticFiles(directory="/media"), name="files")  ##REMOVED WHEN ADDING DIRECT_PLAY

# Templates for HTML rendering
templates = Jinja2Templates(directory="templates")

# Set templates in UI router
from routes.ui import set_templates
set_templates(templates)

# --- AUTHENTICATION MIDDLEWARE ---

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Authentication middleware - checks auth for all requests except public endpoints.
    """
    path = request.url.path
    
    # Public endpoints (no auth required)
    public_endpoints = [
        "/",
        "/api/login",
        "/api/signup",
        "/api/max_users",
        "/api/check_auth",
        "/api/health",
        "/favicon.ico",
        "/install_vlc_link.bat"
    ]
    
    # Check if path is public
    if path in public_endpoints or path.startswith("/static/") or any(path.startswith(ep) for ep in ["/api/login", "/api/signup"]):
        return await call_next(request)
    
    # Static files require auth
    if path.startswith("/cache/") or path.startswith("/files/"):
        username = check_auth(request, check_inactivity=True)
        if not username:
            return Response(status_code=401, content="Unauthorized")
        return await call_next(request)
    
    # All other API endpoints require auth
    username = check_auth(request, check_inactivity=True)
    
    if username:
        import display_filter
        display_filter.CURRENT_USER.set(username)        # per-user display filter (display_filter.py)
        started = time.time()
        response = await call_next(request)
        took = time.time() - started
        if took > 0.5 and path.startswith('/api/'):          # slow-request log, for finding sluggish spots
            print(f"[SLOW] {request.method} {path}?{request.url.query[:120]} {took:.2f}s")
        return response
    else:
        # API requests get JSON error
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401, 
                content={"detail": "Unauthorized or Session Expired"}
            )
        return Response(status_code=401, content="Unauthorized")


# --- DEPENDENCY INJECTION SETUP ---

def inject_dependencies():
    """Inject shared instances into all route modules."""
    
    # Import route modules
    import routes.auth
    import routes.streaming  
    import routes.watch_history_routes
    import routes.cast
    import routes.ui
    import routes.library
    import routes.metadata
    import routes.scanning
    import routes.thumbnails
    import routes.videos
    import routes.files
    import routes.playlists
    
    # Set dependencies for each module
    routes.auth.set_user_model(user_model)
    
    routes.streaming.set_model(model)
    routes.streaming.set_watch_history(watch_history_instance)
    routes.streaming.set_stream_manager(stream_manager)
    
    routes.watch_history_routes.set_model(model)
    routes.watch_history_routes.set_watch_history(watch_history_instance)
    
    routes.cast.set_model(model)
    routes.ui.set_model(model)
    routes.library.set_model(model)
    routes.metadata.set_model(model)
    routes.scanning.set_model(model)
    routes.thumbnails.set_model(model)
    routes.videos.set_model(model)
    routes.videos.set_watch_history(watch_history_instance)
    routes.files.set_model(model)
    
    routes.playlists.set_model(model)
    routes.playlists.set_db(model.db)

    routes.direct_play.set_model(model)  # Added with direct_play

    # New player (roadmap #8): HLS segment manager + VAAPI check
    import routes.player
    routes.player.set_model(model)
    routes.player.start_background()

    import routes.home
    routes.home.set_model(model)
    routes.home.set_watch_history(watch_history_instance)
    import routes.collections
    routes.collections.set_model(model)
    routes.collections.set_watch_history(watch_history_instance)

    # Play counts (Home rows, Most Played / Last Played sorts): table + one-off seed from history
    if getattr(model, 'db', None):
        try:
            import library_stats
            n = library_stats.seed_from_history(model.db.get_connection(), watch_history_instance.history)
            if n:
                print(f"[watch_stats] seeded {n} play counts from watch history")
        except Exception as e:
            print(f"[watch_stats] seed failed: {e}")
    
    # External performer database (StashDB / ThePornDB)
    import ext_db
    ext_db.set_model(model)
    # Inactivity auto-logout setting (Settings -> General)
    if getattr(model, 'db', None):
        from helpers import load_session_settings
        load_session_settings(model.db)
        # Routine library scans (Settings -> General -> Automatic scans)
        import automation
        automation.start_scheduler(model)
        import display_filter
        display_filter.seed_library_types(model)
    # Database maintenance (Settings -> Tools): purge expired deleted files, flags, orphans.
    # Runs shortly after start-up and then daily (can be switched off in Settings).
    import tools_api
    tools_api.set_model(model)
    if getattr(model, 'db', None):
        import db_maintenance
        db_maintenance.start_scheduler(model.db.get_connection)


# --- REGISTER ALL ROUTE MODULES ---

def register_routes():
    """Register all route modules with the FastAPI app."""
    
    # All route modules
    app.include_router(auth_router)
    app.include_router(streaming_router)
    app.include_router(watch_history_router)
    app.include_router(cast_router)
    app.include_router(ext_db_router)
    app.include_router(tools_router)
    app.include_router(ui_router)  # Root routes (/, /install_vlc_link.bat)
    app.include_router(ui_api_router)  # API routes (/api/init, /api/config/save)
    app.include_router(library_router)
    app.include_router(metadata_router)
    app.include_router(scanning_router)
    app.include_router(thumbnails_router)
    app.include_router(videos_router)
    app.include_router(files_router)
    app.include_router(playlists_router)
    app.include_router(direct_play_router) #added this with direct_play
    app.include_router(player_router)
    app.include_router(home_router)
    app.include_router(collections_router)


# --- STATUS ENDPOINT ---

@app.get("/api/health")
def health():
    """Public liveness check (Docker HEALTHCHECK): no data, just that the app answers."""
    from config import VERSION
    return {"status": "ok", "version": VERSION}


@app.get("/api/status")
def get_status():
    """Status endpoint to verify server is running."""
    return {
        "status": "running",
        "endpoints_modularized": 47,
        "endpoints_remaining": 0,
        "total_endpoints": 47,
        "progress": "100%",
        "completed_modules": [
            "auth", "streaming", "watch_history", "cast", "ui",
            "library", "metadata", "scanning", "thumbnails", "videos", "files"
        ],
        "message": "All modules active and functional!"
    }


# --- STARTUP ---

# Inject dependencies before starting
inject_dependencies()

# Register all routes
register_routes()

# Start the server
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Video Library Manager Starting...")
    print("=" * 60)
    print(f"📊 Modularization Status:")
    backup_database()
    print(f"   ✅ Completed: 47 of 47 endpoints (100%)")
    print(f"   ⏳ Remaining: 0 endpoints")
    print(f"")
    print(f"📦 Active Modules:")
    print(f"   ✅ routes/auth.py (6 endpoints)")
    print(f"   ✅ routes/streaming.py (4 endpoints)")
    print(f"   ✅ routes/watch_history_routes.py (5 endpoints)")
    print(f"   ✅ routes/cast.py (3 endpoints)")
    print(f"   ✅ routes/ui.py (4 endpoints)")
    print(f"   ✅ routes/library.py (9 endpoints)")
    print(f"   ✅ routes/metadata.py (5 endpoints)")
    print(f"   ✅ routes/scanning.py (2 endpoints)")
    print(f"   ✅ routes/thumbnails.py (2 endpoints)")
    print(f"   ✅ routes/videos.py (3 endpoints)")
    print(f"   ✅ routes/files.py (4 endpoints)")
    print(f"   ✅ routes/playlists.py (12 endpoints)")
    print(f"")
    print("=" * 60)
    print(f"🌐 Server starting on http://0.0.0.0:8008")
    print("=" * 60)
    print("")
    
    uvicorn.run(app, host="0.0.0.0", port=8008)