# This file is: ./helpers.py

"""
Helper utilities for the Video Library Manager.
All file operations use UTF-8 encoding to prevent mojibake.
"""

import os
import json
import hashlib
import time
from typing import Optional, Dict, Any
from datetime import datetime
from fastapi import Request
from config import INACTIVITY_TIMEOUT, AUTH_COOKIE_NAME, CACHE_DIR
try:
    from config import SHOW_INACTIVITY_TIMER as _SHOW_TIMER_DEFAULT
except ImportError:
    _SHOW_TIMER_DEFAULT = True

# Auto-logout after inactivity: changeable in Settings (stored in the metadata table under
# 'session'). timeout = seconds, 0 = never log out for inactivity; during_playback = keep
# counting while a video plays (off: playing pauses the countdown).
SESSION = {'timeout': INACTIVITY_TIMEOUT, 'show_countdown': _SHOW_TIMER_DEFAULT, 'during_playback': False}
SESSION_KEY = 'session'


def load_session_settings(db):
    """Read the saved inactivity settings (called at start-up)."""
    try:
        row = db.get_connection().execute('SELECT value FROM metadata WHERE key = ?', (SESSION_KEY,)).fetchone()
        if row:
            saved = json.loads(row[0])
            SESSION['timeout'] = max(0, int(saved.get('timeout', SESSION['timeout'])))
            SESSION['show_countdown'] = bool(saved.get('show_countdown', SESSION['show_countdown']))
            SESSION['during_playback'] = bool(saved.get('during_playback', SESSION['during_playback']))
    except Exception as e:
        print(f"Session settings not loaded: {e}")
    return dict(SESSION)


def save_session_settings(db, timeout=None, show_countdown=None, during_playback=None):
    if timeout is not None:
        SESSION['timeout'] = max(0, min(int(timeout), 7 * 24 * 3600))
    if show_countdown is not None:
        SESSION['show_countdown'] = bool(show_countdown)
    if during_playback is not None:
        SESSION['during_playback'] = bool(during_playback)
    conn = db.get_connection()
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SESSION_KEY, json.dumps(SESSION)))
    conn.commit()
    return dict(SESSION)

def load_json_file(filepath: str, default: Any = None) -> Any:
    """
    Load JSON file with proper UTF-8 encoding.
    
    Args:
        filepath: Path to JSON file
        default: Default value if file doesn't exist or has errors
        
    Returns:
        Parsed JSON data or default value
    """
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Error loading {filepath}: {e}")
            return default
    return default


def save_json_file(filepath: str, data: Any, indent: int = 4) -> bool:
    """
    Save data to JSON file with proper UTF-8 encoding.
    
    Args:
        filepath: Path to save JSON file
        data: Data to serialize
        indent: JSON indentation level
        
    Returns:
        True if successful, False otherwise
    """
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving {filepath}: {e}")
        return False


def get_thumbnail_path(cache_id: str) -> Optional[str]:
    """
    Get thumbnail path, checking for WebP first, then falling back to old .dat format.
    
    Args:
        cache_id: Cache identifier for the file
        
    Returns:
        URL path to thumbnail or None if not found
    """
    webp_path = os.path.join(CACHE_DIR, cache_id, "thumb_0.webp")
    dat_path = os.path.join(CACHE_DIR, cache_id, "thumb_0.dat")
    
    if os.path.exists(webp_path):
        return f"/cache/{cache_id}/thumb_0.webp"
    elif os.path.exists(dat_path):
        return f"/cache/{cache_id}/thumb_0.dat"
    return None


def sign_token(username: str) -> str:
    """Start a login session (auth_sessions.py); returns the token for the cookie."""
    import auth_sessions
    return auth_sessions.create(username)


def session_token(request: Request) -> Optional[str]:
    return request.cookies.get(AUTH_COOKIE_NAME)


def check_auth(request: Request, check_inactivity: bool = True) -> Optional[str]:
    """
    The logged-in username, or None.

    The inactivity timeout is always enforced: a session that has run out is deleted and stays
    gone. check_inactivity=False only means "this request doesn't count as activity" (used by
    /api/check_auth, so asking whether you're logged in never keeps a session alive).
    """
    try:
        import auth_sessions
        return auth_sessions.check(session_token(request), SESSION['timeout'], touch=check_inactivity)
    except Exception as e:
        print(f"Session check failed: {e}")
        return None


def request_is_https(request: Request) -> bool:
    """True when the browser reached lustr over HTTPS (directly or through a reverse proxy)."""
    proto = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
    return proto == 'https' or request.url.scheme == 'https'


def same_origin(request: Request) -> bool:
    """For requests that change something: the Origin (or Referer) must be this server.
    A browser always sends one of them on such requests; a missing pair means a non-browser
    client (curl, scripts), which is allowed."""
    from urllib.parse import urlsplit
    source = request.headers.get('origin') or request.headers.get('referer')
    if not source:
        return True
    if source == 'null':
        return False
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or ''
    host = host.split(',')[0].strip().lower()
    try:
        return urlsplit(source).netloc.lower() == host
    except ValueError:
        return False


class LoginLimiter:
    """Brute-force brake: after `limit` wrong passwords from one address within `window`
    seconds, that address can't try again until the window has passed. In memory only."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window = limit, window
        self._fails: Dict[str, list] = {}

    def _recent(self, ip: str, now: float) -> list:
        fails = [t for t in self._fails.get(ip, []) if now - t < self.window]
        if fails:
            self._fails[ip] = fails
        else:
            self._fails.pop(ip, None)
        return fails

    def retry_after(self, ip: str, now: Optional[float] = None) -> int:
        """Seconds until this address may try again (0 = it may try now)."""
        now = time.time() if now is None else now
        fails = self._recent(ip, now)
        if len(fails) < self.limit:
            return 0
        return max(1, int(fails[0] + self.window - now) + 1)

    def failed(self, ip: str, now: Optional[float] = None):
        now = time.time() if now is None else now
        self._recent(ip, now)
        self._fails.setdefault(ip, []).append(now)

    def succeeded(self, ip: str):
        self._fails.pop(ip, None)


login_limiter = LoginLimiter()


def get_file_data(model, path: str) -> Optional[Dict[str, Any]]:
    """
    Get file data from either SQLite or JSON backend.
    
    Args:
        model: LibraryModel instance
        path: File path
        
    Returns:
        File data dictionary or None if not found
    """
    from config import USE_SQLITE
    
    if USE_SQLITE:
        return model.db.get_file(path)
    else:
        return model.files.get(path)


def format_datetime(dt: Optional[datetime]) -> str:
    """
    Format datetime object to string with proper encoding.
    
    Args:
        dt: datetime object or None
        
    Returns:
        Formatted datetime string
    """
    if dt is None:
        return str(datetime.now())
    return str(dt)
