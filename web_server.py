# This file is: ./web_server.py

"""
Video Library Manager - Main Application
==========================================
Streamlined web server that coordinates all route modules.

Original file was 2,215 lines - now reduced to ~250 lines.
All route logic has been moved to focused modules in routes/ folder.
"""

# Import logger FIRST: it sets up logging (console + <data>/logs/lustr.log) and routes print() into it
import logger
from logger import log_info, log_error
import logging

import os
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import glob
from datetime import datetime

# Import configuration
from config import (
    CACHE_DIR, WATCH_HISTORY_DIR, SHOW_INACTIVITY_TIMER, 
    INACTIVITY_TIMEOUT, USE_SQLITE, DATABASE_FILE
)

# Import your existing business logic modules
from model import LibraryModel, UserModel
from media_handler import MediaHandler
from stream_handler import stream_manager
from watch_history import WatchHistory
from search_parser import SearchParser
from cast_manager import CastManager

# Import helper utilities
from helpers import check_auth, check_session, load_json_file, same_origin

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
from routes.captions import router as captions_router
from routes.clips import router as clips_router
# Home / Recommended rows (roadmap #9)
from routes.home import router as home_router
from routes.collections import router as collections_router
from routes.movies import router as movies_router
from routes.images import router as images_router
from routes.tv import router as tv_router
import support


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
        import sqlite3
        src = sqlite3.connect(db_path, timeout=30.0)      # backup API: consistent, and includes what's still in the WAL
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        print(f"Database backed up: {os.path.basename(backup_path)}")
        
        # Keep the newest STARTUP_BACKUPS
        backups = sorted(glob.glob(os.path.join(backup_dir, 'library_[0-9]*.db')))   # not library_before_<migration>_*
        for old in backups[:-STARTUP_BACKUPS]:
            os.remove(old)
    except Exception as e:
        print(f"Backup failed: {e}")

# --- APPLICATION SETUP ---

app = FastAPI(title="Video Library Manager")

@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Anything a route didn't handle: log it once, with its traceback and a short ID the page shows,
    so a problem report can point at this exact entry."""
    error_id = logger.new_error_id()
    logging.getLogger('lustr.error').error(f"[{error_id}] {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
                                           exc_info=exc, extra={'error_id': error_id})
    return JSONResponse(status_code=500, content={
        "detail": f"Something went wrong on the server (error ID {error_id}). "
                  f"Settings → Maintenance → Problem report has the details.",
        "error_id": error_id})


# Compress pages, scripts and JSON (the main page is ~1.8 MB, ~350 KB gzipped). Videos, images,
# partial (Range) responses and anything already compressed are passed through untouched.
from starlette.middleware.gzip import GZipMiddleware, DEFAULT_EXCLUDED_CONTENT_TYPES
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6,
                   exclude_content_types=DEFAULT_EXCLUDED_CONTENT_TYPES + (
                       'application/octet-stream', 'application/x-sqlite3', 'application/vnd.sqlite3',
                       'application/x-tar', 'application/x-7z-compressed', 'application/x-rar-compressed',
                       'application/pdf', 'image/x-icon', 'image/vnd.microsoft.icon'))

# Initialize your existing models
model = LibraryModel()
user_model = UserModel()
# Login sessions live in library.db (auth_sessions.py)
import auth_sessions
auth_sessions.set_db_path(DATABASE_FILE)
watch_history_instance = WatchHistory(WATCH_HISTORY_DIR)
if USE_SQLITE:                                     # full-text search index: built / caught up in the background
    import search_index
    search_index.warm_up(model.db)
    import photo_store                             # movie covers / performer photos: files, not database rows
    photo_store.migrate_background(model.db.get_connection)

# --- STATIC FILE SERVING ---

# Ensure cache directory exists
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)
app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")

# Clips (clips.py): made GIFs / short videos
import clips as _clips
os.makedirs(_clips.CLIPS_DIR, exist_ok=True)
app.mount("/clips", StaticFiles(directory=_clips.CLIPS_DIR), name="clips")

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


@app.get("/favicon.ico")
def favicon():
    """The browser tab / bookmark icon (static/icons; the logo's cursive l)."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(STATIC_DIR, 'icons', 'favicon.ico'), media_type='image/x-icon')


@app.get("/manifest.webmanifest")
def manifest():
    """Name and icons for "Add to Home Screen" / installed shortcuts."""
    return JSONResponse({'name': 'lustr', 'short_name': 'lustr', 'start_url': '/', 'display': 'standalone',
                         'background_color': '#111827', 'theme_color': '#111827',
                         'icons': [{'src': '/static/icons/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                                   {'src': '/static/icons/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}]},
                        media_type='application/manifest+json')


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

    # Requests that change something must come from lustr's own pages, so another website
    # open in the same browser can't act with your login (cross-site request forgery).
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not same_origin(request):
        return JSONResponse(status_code=403, content={"detail": "Request from another site refused"})
    
    # Public endpoints (no auth required)
    public_endpoints = [
        "/",
        "/api/login",
        "/api/signup",
        "/api/max_users",
        "/api/check_auth",
        "/api/health",
        "/api/tv/pair/start",                        # a TV that isn't logged in yet (routes/tv.py)
        "/api/tv/pair/poll",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/install_vlc_link.bat"
    ]
    
    # Check if path is public
    if path in public_endpoints or path.startswith("/static/") or any(path.startswith(ep) for ep in ["/api/login", "/api/signup"]):
        return await call_next(request)
    
    # Static files require auth
    if path.startswith("/cache/") or path.startswith("/files/") or path.startswith("/clips/"):
        username = check_auth(request, check_inactivity=True)
        if not username:
            return Response(status_code=401, content="Unauthorized")
        response = await call_next(request)
        # thumbnails carry ?v=<file time>: when that's this file's own time, a new file is a new URL and the
        # browser may keep this one (hover frames reuse the card's ?v=, so they're only kept when it matches)
        v = request.query_params.get("v")
        if path.startswith("/cache/") and v and response.status_code == 200:
            try:
                f = os.path.realpath(os.path.join(CACHE_DIR, path[len("/cache/"):]))
                if f.startswith(os.path.realpath(CACHE_DIR) + os.sep) and str(int(os.path.getmtime(f))) == v:
                    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
            except (OSError, ValueError):
                pass
        return response
    
    # All other API endpoints require auth
    session = check_session(request, check_inactivity=True)
    username = session[0] if session else None

    if username:
        import display_filter
        # per-user display filter (display_filter.py); a TV app keeps its own, under 'name#tv'
        display_filter.CURRENT_USER.set(username + display_filter.TV_SUFFIX if session[1] == 'tv' else username)
        started = time.time()
        response = await call_next(request)
        took = time.time() - started
        if took > 0.5 and path.startswith('/api/'):          # slow-request log, for finding sluggish spots
            logging.getLogger('lustr.slow').info(f"{request.method} {path}?{request.url.query[:120]} took {took:.2f}s")
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
    support.set_model(model)
    routes.videos.set_watch_history(watch_history_instance)
    routes.files.set_model(model)
    
    routes.playlists.set_model(model)
    routes.playlists.set_db(model.db)

    routes.direct_play.set_model(model)  # Added with direct_play

    # New player (roadmap #8): HLS segment manager + VAAPI check
    import routes.player
    routes.player.set_model(model)
    routes.player.start_background()
    import routes.captions                  # closed captions: worker + night window
    routes.captions.set_model(model)
    routes.captions.start_background()
    import routes.clips                     # clips: table + worker
    routes.clips.set_model(model)
    routes.clips.start_background()

    import routes.home
    routes.home.set_model(model)
    routes.home.set_watch_history(watch_history_instance)
    import routes.collections
    routes.collections.set_model(model)
    routes.collections.set_watch_history(watch_history_instance)
    import routes.movies                    # full movies (movies.py)
    routes.movies.set_model(model)
    import routes.images                    # the Images tab (images.py)
    routes.images.set_model(model)
    routes.images.set_watch_history(watch_history_instance)
    routes.movies.set_watch_history(watch_history_instance)
    if getattr(model, 'db', None):
        try:
            import movies
            movies.ensure_schema(model.db.get_connection())
            n, m = movies.prune(model.db.get_connection())
            if n or m:
                print(f"[movies] {n} entries of deleted videos and {m} empty movies removed")
        except Exception as e:
            print(f"[movies] start-up check failed: {e}")

    # Play counts (Home rows, Most Played / Last Played sorts): table + one-off seed from history
    if getattr(model, 'db', None):
        try:
            import library_stats
            n = library_stats.seed_from_history(model.db.get_connection(), watch_history_instance.history)
            if n:
                print(f"[watch_stats] seeded {n} play counts from watch history")
            n = library_stats.seed_play_log(model.db.get_connection(), watch_history_instance.history)
            if n:
                print(f"[watch_stats] seeded {n} imported Play History entries")
            conn = model.db.get_connection()
            n = library_stats.relink_plays(conn)       # merges / moves since the last start
            if n:
                print(f"[watch_stats] {n} Play History entries followed their files")
            library_stats.snapshot_videos(conn)        # what each played video is, should it be deleted
            conn.commit()
            model.fill_missing_metadata()              # files whose length / codec were never read
        except Exception as e:
            print(f"[watch_stats] seed failed: {e}")
    
    # External performer database (StashDB / ThePornDB)
    import ext_db
    ext_db.set_model(model)
    # Inactivity auto-logout setting (Settings -> General)
    if getattr(model, 'db', None):
        from helpers import load_session_settings
        s = load_session_settings(model.db)
        import auth_sessions
        auth_sessions.purge(s['timeout'])
        # Routine library scans (Settings -> General -> Automatic scans)
        import automation
        automation.start_scheduler(model)
        import display_filter
        display_filter.seed_library_types(model)
        try:                        # .webm and images-only libraries: in Images, not the video library
            import media_kind
            media_kind.migrate(model)
        except Exception as e:
            print(f"[media kind] start-up check failed: {e}")
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
    app.include_router(captions_router)
    app.include_router(clips_router)
    app.include_router(home_router)
    app.include_router(collections_router)
    app.include_router(movies_router)
    app.include_router(images_router)
    app.include_router(tv_router)
    app.include_router(support.router)            # problem report, log level, browser errors


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
    from config import VERSION
    log_info(f"lustr {VERSION} starting on http://0.0.0.0:8008 (log level {logger.current_level()}, "
             f"log file {logger.APP_LOG_FILE})")
    backup_database()
    
    uvicorn.run(app, host="0.0.0.0", port=8008, log_config=None)   # logging is set up by logger.py