# This file is: ./library_stats.py
"""
Play counts and arrival dates for Home rows, collections and the new sorts (roadmap #9).

watch_stats(username, video_id, plays, first_played, last_played)
  A play is counted on the server from the progress posts both players already send
  (POST /api/watch/progress): a viewing session for (user, video) starts after 30 min
  without posts, and counts once the playhead has really moved forward 60 s (or half
  of a video shorter than 2 min). Pauses and big forward jumps don't add up.
  Seeded once from data/watch_history.json (anything finished or watched >= 60 s = 1 play).

play_log(id, username, video_id, path, play_id, started_at, ended_at, played, position, duration, plays, imported)
  The watch log (Settings -> Play History): one row per play. A play is one opening of a video in a
  player (the page sends a play_id and the seconds it was really playing). It's only logged once it
  has played for the minimum (metadata play_log_min_seconds, default 15 s), and counts as a play
  (plays = 1) at 60 s as above. watch_stats.plays / total_played / last_played follow the log, and
  are worked out again when entries are deleted. Seeded once with one 'imported' entry per video.

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
LOG_SEEDED_KEY = 'play_log_seeded'
MIN_LOG_KEY = 'play_log_min_seconds'
DEFAULT_MIN_LOG = 15           # plays shorter than this aren't logged (or counted)
SPECIAL_SORTS = ('recently_added', 'most_played', 'last_played', 'time_watched', 'random')
SORT_ALIASES = {'plays': 'most_played'}       # library column headers -> the sort they mean

_lock = threading.Lock()
_sessions = {}                 # (username, video_id) -> {start, last, pos, advanced}  (posts without a play_id)
_first_seen = {}               # (username, play_id) -> time of its first post
_min_cache = {}
_schema_ready = set()


def arrival_sql(alias='fv', video='v'):
    """SQL for when a video was added to lustr ('YYYY-MM-DD HH:MM...'), comparable as text.
    Needs file_versions as `alias` and videos as `video` in the query."""
    return (f"COALESCE(NULLIF(replace({video}.date_added, 'T', ' '), ''), "
            f"NULLIF(replace({alias}.added_at, 'T', ' '), ''), {alias}.date_created)")


def nonzero_length_sql(alias='fv'):
    """SQL that leaves out files whose length is zero (any of the stored forms); an unknown
    length (not read yet) still shows."""
    return f"COALESCE({alias}.length, '') NOT IN ('0:00', '00:00', '0:00:00', '00:00:00')"


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
    if 'total_played' not in [r[1] for r in conn.execute('PRAGMA table_info(watch_stats)')]:
        conn.execute('ALTER TABLE watch_stats ADD COLUMN total_played REAL NOT NULL DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS play_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        video_id TEXT NOT NULL,
        path TEXT,
        play_id TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        played REAL,
        position REAL,
        duration REAL,
        plays INTEGER NOT NULL DEFAULT 0,
        imported INTEGER NOT NULL DEFAULT 0,
        UNIQUE (username, play_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_play_log_started ON play_log(username, started_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_play_log_video ON play_log(username, video_id)')
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


def _ts(t):
    return datetime.fromtimestamp(t).isoformat(timespec='seconds')


def _bump(conn, username, video_id, plays, when, played=0.0):
    conn.execute('INSERT INTO watch_stats (username, video_id, plays, first_played, last_played, total_played) VALUES (?, ?, ?, ?, ?, ?) '
                 'ON CONFLICT(username, video_id) DO UPDATE SET plays = plays + excluded.plays, last_played = excluded.last_played, '
                 'first_played = COALESCE(first_played, excluded.first_played), '
                 'total_played = COALESCE(total_played, 0) + excluded.total_played',
                 (username, video_id, plays, when if plays else None, when, played))


def min_log_seconds(conn):
    """Plays shorter than this (seconds) aren't logged. Same for everyone."""
    key = id(conn)
    if key not in _min_cache:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (MIN_LOG_KEY,)).fetchone()
        try:
            _min_cache[key] = max(0.0, float(row[0])) if row else float(DEFAULT_MIN_LOG)
        except (TypeError, ValueError):
            _min_cache[key] = float(DEFAULT_MIN_LOG)
    return _min_cache[key]


def set_min_log_seconds(conn, seconds):
    v = max(0, min(3600, int(float(seconds or 0))))
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP', (MIN_LOG_KEY, str(v)))
    conn.commit()
    _min_cache[id(conn)] = float(v)
    return v


def _legacy_play(username, vid, current, now):
    """A post without a play_id (an old page): plays are viewing sessions (30 min gap) and
    'played' is how far the playhead really moved forward. -> (play_id, played)"""
    key = (username, vid)
    s = _sessions.get(key)
    if s is None or now - s['last'] > SESSION_GAP:
        s = _sessions[key] = {'start': now, 'last': now, 'pos': current, 'advanced': 0.0}
    else:
        step = current - s['pos']
        if 0 < step <= (now - s['last']) * 4 + 15:          # real playback (up to 4x speed), not a jump
            s['advanced'] += step
        s['pos'], s['last'] = current, now
    if len(_sessions) > 5000:                               # forget long-finished sessions
        for k in [k for k, v in _sessions.items() if now - v['last'] > SESSION_GAP][:2500]:
            del _sessions[k]
    return f"session:{vid}:{int(s['start'])}", s['advanced']


def record_progress(conn, username, path, current, duration, now=None, play_id=None, played=None):
    """Called for every progress post. play_id / played (seconds really playing) come from the
    player. -> True when this post completed a play."""
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
    with _lock:
        if play_id:
            play_id, played = str(play_id)[:64], max(0.0, float(played or 0))
        else:
            play_id, played = _legacy_play(username, vid, current, now)
        first = _first_seen.setdefault((username, play_id), now)
        if len(_first_seen) > 5000:
            for k in [k for k, t in _first_seen.items() if now - t > 86400][:2500]:
                del _first_seen[k]
        row = conn.execute('SELECT id, played, plays FROM play_log WHERE username = ? AND play_id = ?',
                           (username, play_id)).fetchone()
        when = _ts(now)
        if row is None:
            if played < min_log_seconds(conn):
                return False
            plays = 1 if played >= need else 0
            conn.execute('INSERT INTO play_log (username, video_id, path, play_id, started_at, ended_at, played, position, duration, plays) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (username, vid, path, play_id, _ts(min(first, now - played)), when, played, current, duration, plays))
            d_played, d_plays = played, plays
        else:
            old_played, old_plays = float(row[1] or 0), int(row[2] or 0)
            played = max(played, old_played)
            plays = 1 if (old_plays or played >= need) else 0
            conn.execute('UPDATE play_log SET ended_at = ?, played = ?, position = ?, duration = ?, plays = ? WHERE id = ?',
                         (when, played, current, duration, plays, row[0]))
            d_played, d_plays = played - old_played, plays - old_plays
        _bump(conn, username, vid, d_plays, when, d_played)
        conn.commit()
    return d_plays > 0


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
    """{video_id: (plays, last_played, total_played)} for one user."""
    ensure_schema(conn)
    return {r[0]: (r[1] or 0, r[2] or '', r[3] or 0.0) for r in conn.execute(
        'SELECT video_id, plays, last_played, total_played FROM watch_stats WHERE username = ?', (username or '',))}


def add_play_stats(conn, username, items):
    """Put plays / time_watched (seconds) / last_played on list items (dicts with video_id)."""
    vids = list({i.get('video_id') for i in items if i.get('video_id')})
    st = {}
    if username and vids:
        ensure_schema(conn)
        for k in range(0, len(vids), 500):
            chunk = vids[k:k + 500]
            for r in conn.execute(f'SELECT video_id, plays, total_played, last_played FROM watch_stats '
                                  f'WHERE username = ? AND video_id IN ({",".join("?" * len(chunk))})', [username] + chunk):
                st[r[0]] = r[1:]
    for i in items:
        plays, total, last = st.get(i.get('video_id'), (0, 0.0, ''))
        i['plays'], i['time_watched'], i['last_played'] = plays or 0, round(total or 0), last or ''
    return items


def arrival_map(conn):
    """{video_id: arrival date} from each video's preferred live file."""
    return {r[0]: r[1] or '' for r in conn.execute(
        f'SELECT fv.video_id, MAX({arrival_sql("fv")}) FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
        f'WHERE fv.is_preferred = 1 AND fv.deleted_at IS NULL GROUP BY fv.video_id')}


def sort_field(sort_by):
    """'Recently Added' -> 'recently_added' when it's one of ours, else None."""
    f = (sort_by or '').strip().lower().replace(' ', '_')
    f = SORT_ALIASES.get(f, f)
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
            key = lambda r: st.get(r.get('video_id'), (0, '', 0.0))[:2]
        elif field == 'time_watched':
            key = lambda r: (lambda s: (s[2], s[0]))(st.get(r.get('video_id'), (0, '', 0.0)))
        else:
            key = lambda r: st.get(r.get('video_id'), (0, '', 0.0))[1]
    items.sort(key=key, reverse=desc)
    return items


# ---------------------------------------------------------------------------
# Play history (Settings -> Play History)
# ---------------------------------------------------------------------------
def seed_play_log(conn, history):
    """Once: one 'imported' log entry per video with a play count or watch history.
    Its plays = the video's count so far; how long it played isn't known (played NULL). -> rows written"""
    ensure_schema(conn)
    if conn.execute('SELECT 1 FROM metadata WHERE key = ?', (LOG_SEEDED_KEY,)).fetchone():
        return 0
    paths = {r[0]: r[1] for r in conn.execute('SELECT path, video_id FROM file_versions')}
    preferred = {r[0]: r[1] for r in conn.execute(
        'SELECT video_id, path FROM file_versions WHERE deleted_at IS NULL ORDER BY is_preferred')}
    seen = {}                                        # (user, video_id) -> (when, path, progress, duration)
    for user, entries in (history or {}).items():
        for path, e in (entries or {}).items():
            vid = paths.get(path)
            if not vid or not isinstance(e, dict):
                continue
            when = str(e.get('last_watched') or '')[:19]
            if (user, vid) not in seen or when > seen[(user, vid)][0]:
                seen[(user, vid)] = (when, path, float(e.get('progress') or 0), float(e.get('duration') or 0))
    rows = []
    for user, vid, plays, last in conn.execute('SELECT username, video_id, plays, last_played FROM watch_stats').fetchall():
        h = seen.pop((user, vid), None)
        when = last or (h[0] if h else None)
        rows.append((user, vid, h[1] if h else preferred.get(vid), when, h[2] if h else None, h[3] if h else None, plays or 0))
    for (user, vid), h in seen.items():
        rows.append((user, vid, h[1], h[0] or None, h[2], h[3], 0))
    for user, vid, path, when, pos, dur, plays in rows:
        conn.execute('INSERT OR IGNORE INTO play_log (username, video_id, path, play_id, started_at, ended_at, played, '
                     'position, duration, plays, imported) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 1)',
                     (user, vid, path, f'import:{vid}', when, when, pos, dur, plays))
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (LOG_SEEDED_KEY, _now()))
    conn.commit()
    return len(rows)


_DATE = __import__('re').compile(r'^\d{4}-\d{2}-\d{2}$')


def _cast_sql(col):
    return (f"(SELECT group_concat(vc.value, ', ') FROM video_cast_junction cj JOIN video_cast vc ON vc.id = cj.cast_id "
            f"WHERE cj.video_id = {col})")


def _like(q):
    return '%' + (q or '').strip().lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _order(sorts, sort, desc, default):
    expr = sorts.get(sort) or sorts[default]
    d = 'DESC' if desc else 'ASC'
    return f'{expr} IS NULL, {expr} {d}' if not desc else f'{expr} {d}'


_LOG_SORTS = {
    'cast': f"LOWER(COALESCE({_cast_sql('pl.video_id')}, ''))",
    'title': "LOWER(COALESCE(NULLIF(v.title, ''), pl.path))",
    'library': "LOWER(COALESCE(v.library, ''))",
    'path': 'LOWER(pl.path)',
    'watched_at': 'pl.started_at',
    'played': 'pl.played',
    'progress': 'pl.position',
    'plays': 'pl.plays',
}


def _log_where(username, q, date_from, date_to, counted_only, shown):
    where, params = ['pl.username = ?'], [username or '']
    if date_from and _DATE.match(date_from):
        where.append('substr(pl.started_at, 1, 10) >= ?')
        params.append(date_from)
    if date_to and _DATE.match(date_to):
        where.append('substr(pl.started_at, 1, 10) <= ?')
        params.append(date_to)
    if counted_only:
        where.append('pl.plays > 0')
    if q and q.strip():
        like = _like(q)
        where.append(f"(LOWER(COALESCE(v.title, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(pl.path, '')) LIKE ? ESCAPE '\\' "
                     f"OR LOWER(COALESCE(v.library, '')) LIKE ? ESCAPE '\\' "
                     f"OR LOWER(COALESCE({_cast_sql('pl.video_id')}, '')) LIKE ? ESCAPE '\\')")
        params += [like] * 4
    if shown and shown[0]:
        where.append(shown[0])
        params += list(shown[1])
    return ' AND '.join(where), params


def log_page(conn, username, q='', date_from=None, date_to=None, counted_only=False,
             sort='watched_at', desc=True, limit=100, offset=0, shown=None):
    """The watch log, newest first by default. shown: (sql, params) on pl.video_id / v.library
    from display_filter.shown_sql. -> {items, total, summary}"""
    ensure_schema(conn)
    where, params = _log_where(username, q, date_from, date_to, counted_only, shown)
    base = f'FROM play_log pl LEFT JOIN videos v ON v.video_id = pl.video_id WHERE {where}'
    total, plays, played = conn.execute(
        f'SELECT COUNT(*), COALESCE(SUM(pl.plays), 0), COALESCE(SUM(pl.played), 0) {base}', params).fetchone()
    rows = conn.execute(
        f"SELECT pl.id, pl.video_id, pl.path, pl.started_at, pl.ended_at, pl.played, pl.position, pl.duration, "
        f"pl.plays, pl.imported, v.title, v.library, {_cast_sql('pl.video_id')} {base} "
        f"ORDER BY {_order(_LOG_SORTS, sort, desc, 'watched_at')}, pl.id DESC LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
    keys = ('id', 'video_id', 'path', 'started_at', 'ended_at', 'played', 'position', 'duration',
            'plays', 'imported', 'title', 'library', 'cast')
    return {'items': [dict(zip(keys, r)) for r in rows], 'total': total,
            'summary': {'entries': total, 'plays': plays, 'played': played}}


_PATH_OF = ("(SELECT fv.path FROM file_versions fv WHERE fv.video_id = ws.video_id "
            "ORDER BY fv.deleted_at IS NOT NULL, fv.is_preferred DESC LIMIT 1)")
_STATS_SORTS = {
    'cast': f"LOWER(COALESCE({_cast_sql('ws.video_id')}, ''))",
    'title': f"LOWER(COALESCE(NULLIF(v.title, ''), {_PATH_OF}))",
    'library': "LOWER(COALESCE(v.library, ''))",
    'path': f'LOWER({_PATH_OF})',
    'plays': 'ws.plays',
    'total_played': 'ws.total_played',
    'last_played': 'ws.last_played',
    'first_played': 'ws.first_played',
}


def video_stats_page(conn, username, q='', sort='plays', desc=True, limit=100, offset=0, shown=None):
    """Plays and total time per video. -> {items, total, summary}"""
    ensure_schema(conn)
    where, params = ['ws.username = ?'], [username or '']
    if q and q.strip():
        like = _like(q)
        where.append(f"(LOWER(COALESCE(v.title, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE({_PATH_OF}, '')) LIKE ? ESCAPE '\\' "
                     f"OR LOWER(COALESCE(v.library, '')) LIKE ? ESCAPE '\\' "
                     f"OR LOWER(COALESCE({_cast_sql('ws.video_id')}, '')) LIKE ? ESCAPE '\\')")
        params += [like] * 4
    if shown and shown[0]:
        where.append(shown[0])
        params += list(shown[1])
    base = f"FROM watch_stats ws LEFT JOIN videos v ON v.video_id = ws.video_id WHERE {' AND '.join(where)}"
    total, plays, played = conn.execute(
        f'SELECT COUNT(*), COALESCE(SUM(ws.plays), 0), COALESCE(SUM(ws.total_played), 0) {base}', params).fetchone()
    tie = 'ws.last_played DESC' if sort != 'last_played' else 'ws.video_id'
    rows = conn.execute(
        f"SELECT ws.video_id, {_PATH_OF}, ws.plays, ws.total_played, ws.first_played, ws.last_played, "
        f"v.title, v.library, {_cast_sql('ws.video_id')} {base} "
        f"ORDER BY {_order(_STATS_SORTS, sort, desc, 'plays')}, {tie} LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
    keys = ('video_id', 'path', 'plays', 'total_played', 'first_played', 'last_played', 'title', 'library', 'cast')
    return {'items': [dict(zip(keys, r)) for r in rows], 'total': total,
            'summary': {'videos': total, 'plays': plays, 'played': played}}


def video_totals(conn, username, video_id):
    """{plays, total_played, first_played, last_played, entries} for one video (File Info)."""
    ensure_schema(conn)
    r = conn.execute('SELECT plays, total_played, first_played, last_played FROM watch_stats '
                     'WHERE username = ? AND video_id = ?', (username or '', video_id)).fetchone()
    n = conn.execute('SELECT COUNT(*) FROM play_log WHERE username = ? AND video_id = ?', (username or '', video_id)).fetchone()[0]
    plays, total, first, last = r if r else (0, 0.0, None, None)
    return {'plays': plays or 0, 'total_played': round(total or 0), 'first_played': first, 'last_played': last, 'entries': n}


def _recompute(conn, username, video_id):
    """watch_stats for one video from what's left in its log."""
    n, plays, played, first, last = conn.execute(
        'SELECT COUNT(*), COALESCE(SUM(plays), 0), COALESCE(SUM(played), 0), '
        'MIN(CASE WHEN plays > 0 THEN started_at END), MAX(ended_at) FROM play_log WHERE username = ? AND video_id = ?',
        (username, video_id)).fetchone()
    if not n:
        conn.execute('DELETE FROM watch_stats WHERE username = ? AND video_id = ?', (username, video_id))
        return
    conn.execute('INSERT INTO watch_stats (username, video_id, plays, first_played, last_played, total_played) '
                 'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, video_id) DO UPDATE SET plays = excluded.plays, '
                 'first_played = excluded.first_played, last_played = excluded.last_played, total_played = excluded.total_played',
                 (username, video_id, plays, first, last, played))


def delete_entries(conn, username, ids=None, date_from=None, date_to=None):
    """Delete log entries: these ids, or everything in a date range (none = all). Play counts,
    time and last played are worked out again from what's left. -> number deleted"""
    ensure_schema(conn)
    where, params = ['username = ?'], [username or '']
    if ids is not None:
        ids = [int(i) for i in ids][:10000]
        if not ids:
            return 0
        where.append(f'id IN ({",".join("?" * len(ids))})')
        params += ids
    else:
        if date_from and _DATE.match(date_from):
            where.append('substr(started_at, 1, 10) >= ?')
            params.append(date_from)
        if date_to and _DATE.match(date_to):
            where.append('substr(started_at, 1, 10) <= ?')
            params.append(date_to)
    w = ' AND '.join(where)
    with _lock:
        vids = [r[0] for r in conn.execute(f'SELECT DISTINCT video_id FROM play_log WHERE {w}', params)]
        n = conn.execute(f'DELETE FROM play_log WHERE {w}', params).rowcount
        for vid in vids:
            _recompute(conn, username, vid)
            _sessions.pop((username, vid), None)
        conn.commit()
    return n
