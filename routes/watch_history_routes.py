# This file is: ./routes/watch_history_routes.py

"""
Watch history routes for progress tracking.
"""

import os
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from helpers import check_auth, get_file_data, get_thumbnail_path
from media_handler import MediaHandler

router = APIRouter(prefix="/api/watch", tags=["watch_history"])

# Global references (will be set by web_server.py)
_model = None
_watch_history = None

def get_model():
    """Dependency function to get model."""
    return _model

def get_watch_history():
    """Dependency function to get watch_history."""
    return _watch_history

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model

def set_watch_history(watch_history):
    """Set the watch_history instance."""
    global _watch_history
    _watch_history = watch_history



class UpdateProgressRequest(BaseModel):
    path: str
    current_time: float
    duration: float


@router.post("/progress")
def update_watch_progress(req: UpdateProgressRequest, request: Request, watch_history = Depends(get_watch_history)):
    """Update playback progress."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    watch_history.update_progress(username, req.path, req.current_time, req.duration)
    try:                                   # play counts / last played (Home rows, sorts); never blocks saving
        import library_stats
        if library_stats.record_progress(_model.db.get_connection(), username, req.path, req.current_time, req.duration):
            _home_changed()
    except Exception as e:
        print(f"[watch_stats] {e}")
    return {"status": "ok"}


def _home_changed():
    try:
        import home
        home.invalidate()
    except Exception:
        pass


@router.post("/play")
def record_play_event(req: dict, request: Request, watch_history = Depends(get_watch_history)):
    """Record that a video was played."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    path = req.get('path')
    duration = req.get('duration', 0)
    
    if path:
        watch_history.record_play(username, path, duration)
    
    return {"status": "ok"}


def _hidden_paths(model):
    """Paths hidden by the current user's display filter (display_filter.py)."""
    if not getattr(model, 'db', None):
        return set()
    import display_filter
    return display_filter.hidden_paths(model.db.get_connection(), model)


@router.get("/recently_watched")
def get_recently_watched(request: Request, limit: int = 20, model = Depends(get_model), watch_history = Depends(get_watch_history)):
    """Get recently watched videos."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    hidden = _hidden_paths(model)
    recent = watch_history.get_recently_watched(username, limit * 10 + 50 if hidden else limit)
    recent = [i for i in recent if i['path'] not in hidden][:limit]
    
    enriched = []
    for item in recent:
        video_data = get_file_data(model, item['path'])
        
        if video_data:
            video_data = video_data.copy()
            video_data['path'] = item['path']
            video_data['filename'] = os.path.basename(item['path'])
            video_data['watch_progress'] = item.get('progress', 0)
            video_data['watch_duration'] = item.get('duration', 0)
            video_data['last_watched'] = item.get('last_watched')
            video_data['completed'] = item.get('completed', False)
            # Use cache_id from database, fallback to recompute if missing
            video_data['cache_id'] = video_data.get('cache_id') or MediaHandler._get_cache_key(item['path'])
            video_data['thumb'] = get_thumbnail_path(video_data['cache_id'])
            video_data['supported'] = MediaHandler.is_browser_supported(
                video_data.get('extension', ''), 
                video_data.get('codec', '')
            )
            enriched.append(video_data)
    
    return {"items": enriched}


@router.get("/continue_watching")
def get_continue_watching(request: Request, limit: int = 10, model = Depends(get_model), watch_history = Depends(get_watch_history)):
    """Get in-progress videos."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    hidden = _hidden_paths(model)
    continue_watching = watch_history.get_continue_watching(username, limit * 10 + 50 if hidden else limit)
    continue_watching = [i for i in continue_watching if i['path'] not in hidden][:limit]
    
    enriched = []
    for item in continue_watching:
        video_data = get_file_data(model, item['path'])
        
        if video_data:
            video_data = video_data.copy()
            video_data['path'] = item['path']
            video_data['filename'] = os.path.basename(item['path'])
            video_data['watch_progress'] = item.get('progress', 0)
            video_data['watch_duration'] = item.get('duration', 0)
            video_data['last_watched'] = item.get('last_watched')
            # Use cache_id from database, fallback to recompute if missing
            video_data['cache_id'] = video_data.get('cache_id') or MediaHandler._get_cache_key(item['path'])
            video_data['thumb'] = get_thumbnail_path(video_data['cache_id'])
            video_data['supported'] = MediaHandler.is_browser_supported(
                video_data.get('extension', ''), 
                video_data.get('codec', '')
            )
            enriched.append(video_data)
    
    return {"items": enriched}


@router.get("/progress/{path:path}")
def get_video_progress(path: str, request: Request, watch_history = Depends(get_watch_history)):
    """Get progress for a specific video."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    path = urllib.parse.unquote(path)
    progress = watch_history.get_progress(username, path)
    
    if progress:
        return progress
    return {"progress": 0, "duration": 0}