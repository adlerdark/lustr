# This file is: ./routes/watch_history_routes.py

"""
Watch history routes for progress tracking.
"""

import os
import urllib.parse
from typing import List, Optional
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
    play_id: Optional[str] = None      # one per opening of a video in a player (Play History)
    played: Optional[float] = None     # seconds it has really been playing, skips not counted
    log_only: bool = False             # too early to save a resume point: only update the log


@router.post("/progress")
def update_watch_progress(req: UpdateProgressRequest, request: Request, watch_history = Depends(get_watch_history)):
    """Update playback progress."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not req.log_only:
        watch_history.update_progress(username, req.path, req.current_time, req.duration)
    try:                                   # play log + counts / last played (Home rows, sorts); never blocks saving
        import library_stats
        if library_stats.record_progress(_model.db.get_connection(), username, req.path, req.current_time, req.duration,
                                         play_id=req.play_id, played=req.played):
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


# ---------------------------------------------------------------------------
# Play History (Settings -> Play History): the watch log and plays / time per video
# ---------------------------------------------------------------------------
def _user(request):
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return username


def _shown(model, conn, col):
    import display_filter
    return display_filter.shown_sql(conn, model, col=col, lib_col='v.library')


def _with_video(model, items):
    """Add 'video' (the same shape as /api/videos items, for playing) to each row; None when the file is gone."""
    import home
    for it in items:
        it['video'] = home.card(model, it['path']) if it.get('path') else None
        if it['video'] is None and it.get('video_id'):
            row = model.db.get_connection().execute(
                'SELECT path FROM file_versions WHERE video_id = ? AND deleted_at IS NULL ORDER BY is_preferred DESC LIMIT 1',
                (it['video_id'],)).fetchone()
            it['video'] = home.card(model, row[0]) if row else None
        it['missing'] = it['video'] is None
    return items


@router.get("/log")
def play_log(request: Request, q: str = '', date_from: Optional[str] = None, date_to: Optional[str] = None,
             counted_only: bool = False, sort: str = 'watched_at', desc: bool = True, limit: int = 100, offset: int = 0,
             model = Depends(get_model)):
    """The watch log: one entry per play."""
    username = _user(request)
    import library_stats
    conn = model.db.get_connection()
    page = library_stats.log_page(conn, username, q, date_from, date_to, counted_only, sort, desc, limit, offset,
                                  shown=_shown(model, conn, 'pl.video_id'))
    _with_video(model, page['items'])
    return page


@router.get("/stats")
def play_stats(request: Request, q: str = '', sort: str = 'plays', desc: bool = True, limit: int = 100, offset: int = 0,
               model = Depends(get_model)):
    """Plays and total time watched per video."""
    username = _user(request)
    import library_stats
    conn = model.db.get_connection()
    page = library_stats.video_stats_page(conn, username, q, sort, desc, limit, offset,
                                          shown=_shown(model, conn, 'ws.video_id'))
    _with_video(model, page['items'])
    return page


@router.get("/totals")
def play_totals(request: Request, path: str, model = Depends(get_model)):
    """Plays / time watched / last played for one video (File Info)."""
    username = _user(request)
    import library_stats
    conn = model.db.get_connection()
    vid = library_stats.video_id_for_path(conn, path)
    if not vid:
        return {'plays': 0, 'total_played': 0, 'first_played': None, 'last_played': None, 'entries': 0}
    return library_stats.video_totals(conn, username, vid)


class LogDeleteRequest(BaseModel):
    ids: List[int]


class LogClearRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None


@router.post("/log/delete")
def play_log_delete(req: LogDeleteRequest, request: Request, model = Depends(get_model)):
    """Delete entries; their plays and time come off the video's totals."""
    username = _user(request)
    import library_stats
    n = library_stats.delete_entries(model.db.get_connection(), username, ids=req.ids)
    _home_changed()
    return {'success': True, 'deleted': n}


@router.post("/log/clear")
def play_log_clear(req: LogClearRequest, request: Request, model = Depends(get_model)):
    """Delete every entry in a date range (none = the whole log)."""
    username = _user(request)
    import library_stats
    n = library_stats.delete_entries(model.db.get_connection(), username, date_from=req.date_from, date_to=req.date_to)
    _home_changed()
    return {'success': True, 'deleted': n}


class LogSettingsRequest(BaseModel):
    min_seconds: int


@router.get("/log/settings")
def play_log_settings(request: Request, model = Depends(get_model)):
    _user(request)
    import library_stats
    return {'min_seconds': int(library_stats.min_log_seconds(model.db.get_connection()))}


@router.post("/log/settings")
def play_log_settings_save(req: LogSettingsRequest, request: Request, model = Depends(get_model)):
    """Plays shorter than min_seconds aren't logged (or counted). Same for everyone."""
    _user(request)
    import library_stats
    return {'success': True, 'min_seconds': library_stats.set_min_log_seconds(model.db.get_connection(), req.min_seconds)}


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