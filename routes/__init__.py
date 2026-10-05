# This file is: ./routes/__init__.py

"""
Route modules for the Video Library Manager.
All routes are organized by functionality.
"""

from .auth import router as auth_router
from .streaming import router as streaming_router
from .watch_history_routes import router as watch_history_router
from .cast import router as cast_router
from .ui import router as ui_router, api_router as ui_api_router
from .library import router as library_router
from .metadata import router as metadata_router
from .scanning import router as scanning_router
from .thumbnails import router as thumbnails_router
from .videos import router as videos_router
from .files import router as files_router
from .playlists import router as playlists_router
from .direct_play import router as direct_play_router

__all__ = [
    'auth_router',
    'streaming_router', 
    'watch_history_router',
    'cast_router',
    'ui_router',
    'ui_api_router',
    'library_router',
    'metadata_router',
    'scanning_router',
    'thumbnails_router',
    'videos_router',
    'files_router',
    'playlists_router',
    'direct_play_router',
]