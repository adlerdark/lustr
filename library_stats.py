# This file is: ./library_stats.py
"""
Play counts and arrival dates for Home rows, collections and the new sorts (roadmap #9).

watch_stats(username, video_id, plays, first_played, last_played)
  A play is counted on the server from the progress posts both players already send
  (POST /api/watch/progress): a viewing session for (user, video) starts after 30 min
  without posts, and counts once the playhead has really moved forward 60 s (or half
  of a video shorter than 2 min). Pauses and big forward jumps don't add up.
  Seeded once from data/watch_history.json (anything finished or watched >= 60 s = 1 play).

Date added: when lustr first recorded the video (videos.date_added), else when the file was
added (file_versions.added_at), else the file's own date.

No FastAPI imports.
"""
import hashlib
import threading
import time
from datetime import datetime

SESSION_GAP = 30 * 60          # seconds without progress posts before a new viewing session
MIN_SECONDS = 60               # real playback needed for a play
SEEDED_KEY = 'watch_stats_seeded'
SPECIAL_SORTS = ('recently_added', 'most_played', 'last_played', 'random')

_lock = threading.Lock()
_sessions = {}                 # (username, video_id) -> {last, pos, advanced, counted}
_schema_ready = set()


def arrival_sql(alias='fv', video='v'):
    """SQL for when a video was added to lustr ('YYYY-MM-DD HH:MM...'), comparable as text.
    Needs file_versions as `alias` and videos as `video` in the query."""
    return (f"COALESCE(NULLIF(replace({video}.date_added, 'T', ' '), ''), "
            f"NULLIF(replace({alias}.added_at, 'T', ' '), ''), {alias}.date_created)")


def ensure_schema(conn):
    key = id(conn)
    if key in _schema_ready:
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS watch_stats (
        username TEXT NOT NULL,
        video_id TEXT NOT NULL,
        plays INTEGER NOT NULL DEFAULT 0,
        first_played TEXT,
        last_played TEXT,
        PRIMARY KEY (username, video_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_watch_stats_last ON watch_stats(username, last_played)')
    # "who else has this cast member / site / tag" lookups (recommendations)
    for field in ('cast', 'site', 'tags'):
        try:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info(video_{field}_junction)')]
            fk = next((c for c in cols if c.endswith('_id') and c != 'video_id'), None)
            if fk:
                conn.execute(f'CREATE INDEX IF NOT EXISTS idx_video_{field}_junction_value ON video_{field}_junction({fk}, video_id)')
        except Exception:
            pass
    conn.commit()
    _schema_ready.add(key)


def video_id_for_path(conn, path):
    row = conn.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,)).fetchone()
    return row[0] if row else None


def _now():
    return datetime.now().isoformat(timespec='seconds')


def _bump(conn, username, video_id, plays, when):
    conn.execute('INSERT INTO watch_stats (username, video_id, plays, first_played, last_played) VALUES (?, ?, ?, ?, ?) '
                 'ON CONFLICT(username, video_id) DO UPDATE SET plays = plays + excluded.plays, last_played = excluded.last_played, '
                 'first_played = COALESCE(first_played, excluded.first_played)',
                 (username, video_id, plays, when if plays else None, when))


def record_progress(conn, username, path, current, duration, now=None):
    """Called for every progress post. -> True when this post completed a play."""
    if not username or not path:
        return False
    ensure_schema(conn)
    vid = video_id_for_path(conn, path)
    if not vid:
        return False
    now = time.time() if now is None else now
    current = max(0.0, float(current or 0))
    duration = float(duration or 0)
    need = min(MIN_SECONDS, duration / 2) if 0 < duration < 2 * MIN_SECONDS else MIN_SECONDS
    counted = False
    with _lock:
        key = (username, vid)
        s = _sessions.get(key)
        if s is None or now - s['last'] > SESSION_GAP:
            s = _sessions[key] = {'last': now, 'pos': current, 'advanced': 0.0, 'counted': False}
        else:
            step = current - s['pos']
            if 0 < step <= (now - s['last']) * 4 + 15:      # real playback (up to 4x speed), not a jump
                s['advanced'] += step
            s['pos'], s['last'] = current, now
        if not s['counted'] and s['advanced'] >= need:
            s['counted'] = counted = True
        if len(_sessions) > 5000:                           # forget long-finished sessions
            for k in [k for k, v in _sessions.items() if now - v['last'] > SESSION_GAP][:2500]:
                del _sessions[k]
    _bump(conn, username, vid, 1 if counted else 0, datetime.fromtimestamp(now).isoformat(timespec='seconds'))
    conn.commit()
    return counted


def seed_from_history(conn, history):
    """Once: plays = 1 for every history entry that was finished or watched >= 60 s.
    history: {username: {path: {last_watched, progress, duration, completed}}}. -> rows written"""
    ensure_schema(conn)
    if conn.execute('SELECT 1 FROM metadata WHERE key = ?', (SEEDED_KEY,)).fetchone():
        return 0
    paths = {r[0]: r[1] for r in conn.execute('SELECT path, video_id FROM file_versions')}
    n = 0
    for user, entries in (history or {}).items():
        best = {}
        for path, e in (entries or {}).items():
            vid = paths.get(path)
            if not vid or not isinstance(e, dict):
                continue
            if not (e.get('completed') or float(e.get('progress') or 0) >= MIN_SECONDS):
                continue
            when = str(e.get('last_watched') or '')[:19]
            if vid not in best or when > best[vid]:
                best[vid] = when
        for vid, when in best.items():
            conn.execute('INSERT OR IGNORE INTO watch_stats (username, video_id, plays, first_played, last_played) '
                         'VALUES (?, ?, 1, ?, ?)', (user, vid, when or None, when or None))
            n += 1
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (SEEDED_KEY, _now()))
    conn.commit()
    return n


def stats_map(conn, username):
    """{video_id: (plays, last_played)} for one user."""
    ensure_schema(conn)
    return {r[0]: (r[1] or 0, r[2] or '') for r in conn.execute(
        'SELECT video_id, plays, last_played FROM watch_stats WHERE username = ?', (username or '',))}


def arrival_map(conn):
    """{video_id: arrival date} from each video's preferred live file."""
    return {r[0]: r[1] or '' for r in conn.execute(
        f'SELECT fv.video_id, MAX({arrival_sql("fv")}) FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
        f'WHERE fv.is_preferred = 1 AND fv.deleted_at IS NULL GROUP BY fv.video_id')}


def sort_field(sort_by):
    """'Recently Added' -> 'recently_added' when it's one of ours, else None."""
    f = (sort_by or '').strip().lower().replace(' ', '_')
    return f if f in SPECIAL_SORTS else None


def special_sort(conn, items, field, username, seed=0, desc=True):
    """Sort a list of dicts with 'video_id' (and 'path') in place by one of SPECIAL_SORTS.
    'desc' means newest / most first; random ignores it and is stable for one seed."""
    items.sort(key=lambda r: r.get('path') or '')
    if field == 'random':
        salt = str(seed or 0)
        items.sort(key=lambda r: hashlib.md5((salt + str(r.get('video_id'))).encode()).hexdigest())
        return items
    if field == 'recently_added':
        arr = arrival_map(conn)
        key = lambda r: arr.get(r.get('video_id'), '')
    else:
        st = stats_map(conn, username)
        if field == 'most_played':
            key = lambda r: st.get(r.get('video_id'), (0, ''))
        else:
            key = lambda r: st.get(r.get('video_id'), (0, ''))[1]
    items.sort(key=key, reverse=desc)
    return items
