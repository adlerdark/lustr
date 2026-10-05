# This file is: ./helpers.py

"""
Helper utilities for the Video Library Manager.
All file operations use UTF-8 encoding to prevent mojibake.
"""

import os
import json
import hashlib
import time
import hmac
from typing import Optional, Dict, Any
from datetime import datetime
from fastapi import Request
from config import SECRET_KEY, INACTIVITY_TIMEOUT, AUTH_COOKIE_NAME, CACHE_DIR
try:
    from config import SHOW_INACTIVITY_TIMER as _SHOW_TIMER_DEFAULT
except ImportError:
    _SHOW_TIMER_DEFAULT = True

# Auto-logout after inactivity: changeable in Settings (stored in the metadata table under
# 'session'). timeout = seconds, 0 = never log out for inactivity.
SESSION = {'timeout': INACTIVITY_TIMEOUT, 'show_countdown': _SHOW_TIMER_DEFAULT}
SESSION_KEY = 'session'


def load_session_settings(db):
    """Read the saved inactivity settings (called at start-up)."""
    try:
        row = db.get_connection().execute('SELECT value FROM metadata WHERE key = ?', (SESSION_KEY,)).fetchone()
        if row:
            saved = json.loads(row[0])
            SESSION['timeout'] = max(0, int(saved.get('timeout', SESSION['timeout'])))
            SESSION['show_countdown'] = bool(saved.get('show_countdown', SESSION['show_countdown']))
    except Exception as e:
        print(f"Session settings not loaded: {e}")
    return dict(SESSION)


def save_session_settings(db, timeout=None, show_countdown=None):
    if timeout is not None:
        SESSION['timeout'] = max(0, min(int(timeout), 7 * 24 * 3600))
    if show_countdown is not None:
        SESSION['show_countdown'] = bool(show_countdown)
    conn = db.get_connection()
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SESSION_KEY, json.dumps(SESSION)))
    conn.commit()
    return dict(SESSION)

# Session/Token Management
auth_tokens: Dict[str, Dict[str, Any]] = {}


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
    """
    Creates a signed, temporary session token using HMAC.
    
    Args:
        username: Username to create token for
        
    Returns:
        Signed token string
    """
    timestamp = str(int(time.time()))
    msg = f"{username}|{timestamp}"
    signature = hmac.new(SECRET_KEY.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
    token = f"{msg}|{signature}"
    auth_tokens[signature] = {"username": username, "last_active": time.time()}
    return token


def check_auth(request: Request, check_inactivity: bool = True) -> Optional[str]:
    """
    Validates the session token and returns username if valid.
    
    Args:
        request: FastAPI request object
        check_inactivity: Whether to enforce inactivity timeout
        
    Returns:
        Username if authenticated, None otherwise
    """
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    
    try:
        parts = token.split('|')
        if len(parts) != 3:
            return None
        username, timestamp, signature = parts[0], parts[1], parts[2]
        
        # Verify signature
        msg = f"{username}|{timestamp}"
        expected_sig = hmac.new(SECRET_KEY.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
            
        # Verify server state
        if signature not in auth_tokens:
            return None
            
        session = auth_tokens[signature]
        current_time = time.time()
        
        # Check inactivity (sliding window); a timeout of 0 means "never"
        if check_inactivity and SESSION['timeout'] > 0:
            if current_time - session['last_active'] > SESSION['timeout']:
                del auth_tokens[signature]
                return None
        
        # Update activity
        session['last_active'] = current_time
        return username
        
    except Exception:
        return None


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
