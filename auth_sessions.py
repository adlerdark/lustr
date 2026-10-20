# This file is: ./auth_sessions.py
"""
Login sessions, kept in the `sessions` table of library.db.

The cookie holds a random token; only its SHA-256 is stored. A session ends when:
- the owner logs out (or changes / resets the password),
- nothing has happened for longer than the inactivity timeout (Settings -> General),
- it is older than MAX_AGE, whatever the timeout (so "Never" still ends after 30 days),
- it has its own end time (`expires`) and that has passed: TV sessions last at most 4 hours.

kind is 'browser' or 'tv' (a TV linked with a code, tv_pairing.py); device names a TV in the web app.
An ended session is deleted and can never be revived.
"""
import hashlib
import secrets
import sqlite3
import threading
import time
from typing import Optional

MAX_AGE = 30 * 24 * 3600          # hard cap on a session's life, in seconds
TOUCH_EVERY = 15                  # write last_active at most this often (video segments arrive in bursts)

_local = threading.local()
_db_path = None


def set_db_path(path: str):
    """Use this database file (called at start-up; tests point it at a scratch file)."""
    global _db_path
    _db_path = path
    _local.__dict__.clear()
    conn = _conn()
    conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
                        token_hash  TEXT PRIMARY KEY,
                        username    TEXT NOT NULL,
                        created     REAL NOT NULL,
                        last_active REAL NOT NULL)''')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
    for col, decl in (('kind', "TEXT NOT NULL DEFAULT 'browser'"), ('expires', 'REAL'), ('device', 'TEXT')):
        if col not in cols:
            conn.execute(f'ALTER TABLE sessions ADD COLUMN {col} {decl}')
    conn.commit()


def _conn():
    c = getattr(_local, 'conn', None)
    if c is None or getattr(_local, 'path', None) != _db_path:
        c = sqlite3.connect(_db_path, timeout=30.0)
        _local.conn, _local.path = c, _db_path
    return c


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def create(username: str, kind: str = 'browser', max_age: Optional[int] = None, device: Optional[str] = None) -> str:
    """Start a session; returns the token for the cookie. max_age: it ends that many seconds from now."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _conn()
    conn.execute('INSERT INTO sessions (token_hash, username, created, last_active, kind, expires, device) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?)',
                 (_hash(token), username, now, now, kind, now + max_age if max_age else None, device))
    conn.commit()
    return token


def check(token: Optional[str], timeout: int, touch: bool = True) -> Optional[str]:
    """The session's username, or None if there is none or it has ended.
    timeout = inactivity limit in seconds (0 = none). touch=False checks without counting as activity."""
    s = check_session(token, timeout, touch)
    return s[0] if s else None


def check_session(token: Optional[str], timeout: int, touch: bool = True) -> Optional[tuple]:
    """Like check(), but -> (username, kind) or None."""
    if not token or _db_path is None:
        return None
    h = _hash(token)
    conn = _conn()
    row = conn.execute('SELECT username, created, last_active, kind, expires FROM sessions WHERE token_hash = ?',
                       (h,)).fetchone()
    if not row:
        return None
    username, created, last_active, kind, expires = row
    now = time.time()
    if now - created > MAX_AGE or (timeout > 0 and now - last_active > timeout) or (expires is not None and now > expires):
        conn.execute('DELETE FROM sessions WHERE token_hash = ?', (h,))
        conn.commit()
        return None
    if touch and now - last_active >= TOUCH_EVERY:
        conn.execute('UPDATE sessions SET last_active = ? WHERE token_hash = ?', (now, h))
        conn.commit()
    return username, kind or 'browser'


def end(token: Optional[str]):
    """Log this session out."""
    if token and _db_path is not None:
        conn = _conn()
        conn.execute('DELETE FROM sessions WHERE token_hash = ?', (_hash(token),))
        conn.commit()


def end_all(username: Optional[str] = None, keep: Optional[str] = None) -> int:
    """End every session (of one user, if given), except the `keep` token. Returns how many ended."""
    if _db_path is None:
        return 0
    sql, args = 'DELETE FROM sessions WHERE 1=1', []
    if username is not None:
        sql += ' AND username = ?'
        args.append(username)
    if keep:
        sql += ' AND token_hash != ?'
        args.append(_hash(keep))
    conn = _conn()
    n = conn.execute(sql, args).rowcount
    conn.commit()
    return n


def purge(timeout: int) -> int:
    """Delete sessions that have ended (run at start-up)."""
    if _db_path is None:
        return 0
    now = time.time()
    conn = _conn()
    n = conn.execute('DELETE FROM sessions WHERE created < ? OR (? > 0 AND last_active < ?) OR expires < ?',
                     (now - MAX_AGE, timeout, now - timeout, now)).rowcount
    conn.commit()
    return n


def list_sessions(username: str, kind: Optional[str] = None) -> list:
    """An account's sessions, newest first. id = the start of the token's hash (never the token)."""
    if _db_path is None:
        return []
    sql, args = 'SELECT token_hash, kind, device, created, last_active, expires FROM sessions WHERE username = ?', [username]
    if kind:
        sql += ' AND kind = ?'
        args.append(kind)
    rows = _conn().execute(sql + ' ORDER BY created DESC', args).fetchall()
    return [{'id': h[:12], 'kind': k or 'browser', 'device': d, 'created': c, 'last_active': la, 'expires': e}
            for h, k, d, c, la, e in rows]


def end_by_id(username: str, sid: str) -> bool:
    """Log out one of the account's sessions, by its list_sessions id."""
    if _db_path is None or not sid or len(sid) != 12 or not all(ch in '0123456789abcdef' for ch in sid):
        return False
    conn = _conn()
    n = conn.execute('DELETE FROM sessions WHERE username = ? AND substr(token_hash, 1, 12) = ?', (username, sid)).rowcount
    conn.commit()
    return n > 0
