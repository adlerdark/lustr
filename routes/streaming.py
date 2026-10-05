# This file is: ./routes/streaming.py

"""
Streaming routes for HLS video playback.
"""

import os
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from helpers import check_auth, get_file_data

# Import logging
try:
    from logger import log_info, log_debug, log_error
except ImportError:
    def log_info(msg): print(f"[INFO] {msg}")
    def log_debug(msg): print(f"[DEBUG] {msg}")
    def log_error(msg): print(f"[ERROR] {msg}")

router = APIRouter(prefix="/api/stream", tags=["streaming"])

# Global references (will be set by web_server.py)
_model = None
_watch_history = None
_stream_manager = None

def get_model():
    """Dependency function to get model."""
    return _model

def get_watch_history():
    """Dependency function to get watch_history."""
    return _watch_history

def get_stream_manager():
    """Dependency function to get stream_manager."""
    return _stream_manager

def set_model(model):
    """Set the model instance."""
    global _model
    _model = model

def set_watch_history(watch_history):
    """Set the watch_history instance."""
    global _watch_history
    _watch_history = watch_history

def set_stream_manager(stream_manager):
    """Set the stream_manager instance."""
    global _stream_manager
    _stream_manager = stream_manager



class StartStreamRequest(BaseModel):
    path: str
    start_offset: float = 0.0


@router.post("/start")
def start_stream(req: StartStreamRequest, request: Request, model = Depends(get_model), watch_history = Depends(get_watch_history), stream_manager = Depends(get_stream_manager)):
    """Start HLS transcoding session."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    file_data = get_file_data(model, req.path)
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
        
    codec = file_data.get('codec', '')
    duration = file_data.get('duration', 0)
    
    watch_history.record_play(username, req.path, duration)
    
    session_id = stream_manager.start_session(req.path, codec, req.start_offset)
    if not session_id:
        raise HTTPException(status_code=500, detail="Failed to start transcoder")
        
    return {"session_id": session_id}


@router.post("/stop")
def stop_stream(req: dict, stream_manager = Depends(get_stream_manager)):
    """Stop transcoding session."""
    session_id = req.get('session_id')
    if session_id:
        log_info(f"Received stop request for session {session_id}")
        stream_manager.stop_session(session_id)
        log_info(f"Session {session_id} stopped")
    return {"status": "ok"}


@router.post("/cleanup-all")
def cleanup_all_streams(stream_manager = Depends(get_stream_manager)):
    """Emergency cleanup - kill ALL FFmpeg processes."""
    log_info("Received cleanup-all request")
    stream_manager.cleanup_all_sessions()
    log_info("All sessions cleaned up")
    return {"status": "ok"}


@router.get("/{session_id}/playlist.m3u8")
def get_stream_playlist(session_id: str, stream_manager = Depends(get_stream_manager)):
    """Get HLS playlist."""
    playlist_path = stream_manager.get_playlist_path(session_id)
    
    retries = 10
    while retries > 0:
        if playlist_path and os.path.exists(playlist_path):
            return FileResponse(playlist_path, media_type="application/vnd.apple.mpegurl")
        time.sleep(0.5)
        retries -= 1
        
    raise HTTPException(status_code=404, detail="Playlist not ready")


@router.get("/{session_id}/{segment_name}")
def get_stream_segment(session_id: str, segment_name: str, stream_manager = Depends(get_stream_manager)):
    """Get video segment."""
    path = stream_manager.get_segment_path(session_id, segment_name)
    if path and os.path.exists(path):
        return FileResponse(path, media_type="video/MP2T")
    raise HTTPException(status_code=404, detail="Segment not found")