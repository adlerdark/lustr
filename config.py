# This file is: ./config.py
"""
lustr settings. Defaults suit the Docker image (code in /app, data in /app/data);
environment variables override them:

  LUSTR_DATA_DIR     where the database, cache, users and backups live   (default: /app/data)
  LUSTR_SECRET_KEY   key for signing session cookies                    (default: generated once
                     and kept in <data>/.secret_key)
  LUSTR_MEDIA_ROOTS  comma-separated folders inside the container that may hold libraries
                     (default: /media,/media0 - mount your media there)
"""
import os
import secrets

VERSION = "0.1.0"

# --- PATHS ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('LUSTR_DATA_DIR') or os.path.join(SCRIPT_DIR, 'data')
DATA_FILE = os.path.join(DATA_DIR, 'library_data.json')
CACHE_DIR = os.path.join(DATA_DIR, 'cache')
USER_FILE = os.path.join(DATA_DIR, 'users.json')      # user accounts
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# Folders (inside the container) that libraries can be made from; the folder browser and
# direct play only allow paths under these.
MEDIA_ROOTS = [p.strip().rstrip('/') or '/' for p in
               (os.environ.get('LUSTR_MEDIA_ROOTS') or '/media,/media0').split(',') if p.strip()]

# --- AUTHENTICATION SETTINGS ---
# Maximum number of user accounts.
MAX_USERS = 1

# Session timeout in seconds (15 minutes = 900 seconds)
INACTIVITY_TIMEOUT = 900
COOKIE_MAX_AGE = 30 * 24 * 3600

# Show/Hide inactivity countdown timer in the UI
SHOW_INACTIVITY_TIMER = True


def _secret_key():
    key = os.environ.get('LUSTR_SECRET_KEY')
    if key:
        return key
    path = os.path.join(DATA_DIR, '.secret_key')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
    except OSError:
        key = ''
    if not key:
        key = secrets.token_urlsafe(48)
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(key)
            os.chmod(path, 0o600)
        except OSError:
            pass                          # read-only data dir: a new key each start (sessions reset)
    return key


# Key for signing session cookies
SECRET_KEY = _secret_key()
AUTH_COOKIE_NAME = "video_library_session"

# Supported Extensions
VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv',
    '.flv', '.webm', '.m4v', '.mpg', '.mpeg'
}

# Columns that cannot be edited by the user
READ_ONLY_COLUMNS = [
    "Filename", "Path", "Date Created", "Date Modified", "Fav",
    "Length", "Total Bitrate", "File Size", "Date Added", "Date Edited"
]

# Columns that contain lists (comma separated)
LIST_FIELDS = [
    "Site", "Tags", "Cast", "Ethnicity", "Nationality", "Hair", "Body",
    "Tits", "Ass", "Face", "Cock", "Outfit", "Theme",
    "Feature", "Cumshot", "Participants", "Orientation"
]

# Watch history directory (same as DATA_FILE directory)
WATCH_HISTORY_DIR = os.path.dirname(DATA_FILE)

# --- DATABASE SETTINGS ---
USE_SQLITE = True
DATABASE_FILE = os.path.join(DATA_DIR, 'library.db')

# --- ZERO LENGTH VIDEO SETTINGS ---
# Default behavior for hiding videos with 0:00 length
# "hide" = hide by default, "show" = show by default
HIDE_ZERO_LENGTH_DEFAULT = "hide"

# --- THUMBNAIL SETTINGS ---
# WebP quality (0-100, higher = better quality but larger file size)
THUMBNAIL_QUALITY = 75
# Number of preview frames to generate (more = smoother preview but more storage)
THUMBNAIL_FRAMES = 8
# --- FILE CHANGES TRACKING SETTINGS ---
# Number of days to keep deleted file metadata before permanent deletion
DELETED_FILE_RETENTION_DAYS = 7
# Number of days before auto-cleaning up file change flags
FLAG_AUTO_CLEANUP_DAYS = 14
