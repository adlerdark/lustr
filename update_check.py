# This file is: ./update_check.py
"""
Is there a newer lustr? Compares the running version (config.VERSION) with the highest vX.Y.Z tag of
the GitHub repo (LUSTR_UPDATE_REPO, default adlerdark/lustr). The repo has tags, not GitHub releases.

On by default; Settings -> Maintenance turns it off. The check runs in the background at most every
CHECK_EVERY seconds (a page asking never waits for GitHub), sends nothing but a plain request for the
tag list, and keeps its result in the metadata table under 'update_check':
    {enabled, latest, checked_at, error}
No FastAPI imports.
"""
import json
import os
import re
import threading
import time

import requests

from config import VERSION

REPO = os.environ.get('LUSTR_UPDATE_REPO', 'adlerdark/lustr').strip().strip('/')
CHECK_EVERY = 12 * 3600
RETRY_AFTER_ERROR = 3600
TIMEOUT = 10
KEY = 'update_check'
_TAG = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
_lock = threading.Lock()
_running = False


def parse_version(tag):
    """'v0.26.10' / '0.26.10' -> (0, 26, 10); anything else -> None."""
    m = _TAG.match(str(tag or '').strip())
    return tuple(int(x) for x in m.groups()) if m else None


def latest_of(tags):
    """The highest version among tag names -> 'X.Y.Z', or None."""
    versions = [v for v in (parse_version(t) for t in tags) if v]
    return '.'.join(map(str, max(versions))) if versions else None


def is_newer(latest, current=VERSION):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def release_url(version):
    return f'https://github.com/{REPO}/releases/tag/v{version}'


def load(conn):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    return {'enabled': saved.get('enabled', True) is not False, 'latest': saved.get('latest'),
            'checked_at': saved.get('checked_at'), 'error': saved.get('error')}


def save(conn, state):
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (KEY, json.dumps(state)))
    conn.commit()


def fetch_tags():
    """Every tag name of the repo (GitHub API; up to 100, the newest first)."""
    r = requests.get(f'https://api.github.com/repos/{REPO}/tags', params={'per_page': 100}, timeout=TIMEOUT,
                     headers={'User-Agent': f'lustr/{VERSION}', 'Accept': 'application/vnd.github+json'})
    r.raise_for_status()
    return [t.get('name', '') for t in r.json() if isinstance(t, dict)]


def is_stale(state, now=None):
    if not state.get('checked_at'):
        return True
    age = (now or time.time()) - float(state['checked_at'])
    return age >= (RETRY_AFTER_ERROR if state.get('error') else CHECK_EVERY)


def check_now(conn, fetch=fetch_tags):
    """Ask GitHub now and keep the result (an error is kept too, never raised) -> the new state."""
    state = load(conn)
    try:
        latest = latest_of(fetch())
        state.update(latest=latest or state.get('latest'), error=None if latest else 'No version tags found')
    except Exception as e:
        state['error'] = str(e)[:200]
    state['checked_at'] = time.time()
    save(conn, state)
    return state


def refresh_in_background(conn_factory, fetch=fetch_tags):
    """Start a check unless one is running, it's off, or the last one is recent enough -> True if started."""
    global _running
    conn = conn_factory()
    state = load(conn)
    if not state['enabled'] or not is_stale(state):
        return False
    with _lock:
        if _running:
            return False
        _running = True

    def run():
        global _running
        try:
            check_now(conn_factory(), fetch)
        finally:
            with _lock:
                _running = False
    threading.Thread(target=run, name='update-check', daemon=True).start()
    return True


def status(conn):
    """What the page shows: {enabled, current, latest, newer, url, checked_at, error, repo}."""
    state = load(conn)
    latest = state.get('latest')
    newer = state['enabled'] and is_newer(latest)
    return {'enabled': state['enabled'], 'current': VERSION, 'latest': latest, 'newer': newer,
            'url': release_url(latest) if newer else f'https://github.com/{REPO}',
            'checked_at': state.get('checked_at'), 'error': state.get('error'), 'repo': REPO}


def set_enabled(conn, enabled):
    state = load(conn)
    state['enabled'] = bool(enabled)
    save(conn, state)
    return state
