# This file is: ./home.py
"""
Home and Recommended rows (roadmap #9). Each row is one SQL query with ORDER BY ... LIMIT,
scoped to a library / group (or everything), with the viewer's display filter applied.

Rows: continue (in progress), recently_added, recently_watched, because (videos sharing
cast / site / tags with what you played last), most_played, favorites, rediscover (random
unwatched) and collections marked "show on Home" (see collections.py).

Row order / visibility live in metadata 'home_rows'. Results are cached for 60 s per
(user, library, display filter); progress posts and edits call invalidate().

No FastAPI imports.
"""
import json
import os
import threading
import time

import library_stats

ROWS = {
    'continue': 'Continue Watching',
    'recently_added': 'Recently Added',
    'recently_watched': 'Recently Watched',
    'because': 'Because you watched',
    'most_played': 'Most Played',
    'favorites': 'Favorites',
    'rediscover': 'Rediscover',
    'collections': 'Collections on Home',
}
DEFAULT_ORDER = list(ROWS)
SETTINGS_KEY = 'home_rows'
ROW_SIZE = 24
BECAUSE_SEEDS = 2
COMMON_TAG = 2000          # tags on more videos than this say little about similarity (and are slow to score)
CACHE_SECONDS = 60
EVERYTHING = ('Home', 'All Videos', '', None)

_cache = {}
_cache_lock = threading.Lock()


def invalidate():
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def get_settings(conn):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    order = [k for k in saved.get('order') or [] if k in ROWS]
    order += [k for k in DEFAULT_ORDER if k not in order]
    hidden = [k for k in saved.get('hidden') or [] if k in ROWS]
    return {'order': order, 'hidden': hidden}


def save_settings(conn, order=None, hidden=None):
    s = get_settings(conn)
    if order is not None:
        s['order'] = [k for k in order if k in ROWS] + [k for k in DEFAULT_ORDER if k not in order]
    if hidden is not None:
        s['hidden'] = [k for k in hidden if k in ROWS]
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(s)))
    conn.commit()
    invalidate()
    return s


# ---------------------------------------------------------------------------
# scope + cards
# ---------------------------------------------------------------------------
def scope_sql(model, conn, library):
    """(sql, params) to AND in: the library / group, and what this user may see."""
    import display_filter
    parts, params = ['fv.is_preferred = 1', 'fv.deleted_at IS NULL'], []
    if library not in EVERYTHING:
        groups = getattr(model, 'library_groups', None) or {}
        libs = model.get_libraries_in_group(library) if library in groups else [library]
        parts.append(f'v.library IN ({",".join("?" * len(libs)) or "NULL"})')
        params += list(libs)
    sql, p = display_filter.shown_sql(conn, model)
    if sql:
        parts.append(sql)
        params += p
    return ' AND '.join(parts), params


def card(model, path, username=None, watch_history=None, extra=None):
    """One video in the same shape as /api/videos items (thumb, cache_id, supported, progress)."""
    from helpers import get_thumbnail_path
    from media_handler import MediaHandler
    d = model.db.get_file(path)
    if not d:
        return None
    item = dict(d)
    cache_id = item.get('cache_id') or MediaHandler._get_cache_key(path)
    item.update({'type': 'file', 'path': path, 'filename': os.path.basename(path), 'cache_id': cache_id,
                 'thumb': get_thumbnail_path(cache_id),
                 'supported': MediaHandler.is_browser_supported(item.get('extension', ''), item.get('codec', ''))})
    if username and watch_history is not None:
        pr = watch_history.get_progress(username, path)
        if pr:
            item['watch_progress'] = pr.get('progress', 0)
            item['watch_duration'] = pr.get('duration', 0)
    if extra:
        item.update(extra)
    return item


def _paths(conn, sql, params):
    return [r[0] for r in conn.execute(sql, params)]


def _base(where):
    return f'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id WHERE {where}'


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def row_recently_added(conn, where, params, username, n):
    return _paths(conn, _base(where) + f' ORDER BY {library_stats.arrival_sql("fv")} DESC, fv.path LIMIT ?', params + [n])


def row_recently_watched(conn, where, params, username, n):
    return _paths(conn, 'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                        'JOIN watch_stats ws ON ws.video_id = v.video_id AND ws.username = ? '
                        f'WHERE {where} AND ws.last_played IS NOT NULL ORDER BY ws.last_played DESC LIMIT ?',
                  [username or ''] + params + [n])


def row_most_played(conn, where, params, username, n):
    return _paths(conn, 'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                        'JOIN watch_stats ws ON ws.video_id = v.video_id AND ws.username = ? '
                        f'WHERE {where} AND ws.plays > 0 ORDER BY ws.plays DESC, ws.last_played DESC LIMIT ?',
                  [username or ''] + params + [n])


def row_favorites(conn, where, params, username, n):
    return _paths(conn, _base(where) + f' AND v.favorite = 1 ORDER BY {library_stats.arrival_sql("fv")} DESC LIMIT ?',
                  params + [n])


def _unwatched(alias='v'):
    return (f'NOT EXISTS (SELECT 1 FROM watch_stats w2 WHERE w2.video_id = {alias}.video_id '
            f'AND w2.username = ? AND w2.plays > 0)')


def row_rediscover(conn, where, params, username, n):
    return _paths(conn, _base(where) + f' AND {_unwatched()} ORDER BY RANDOM() LIMIT ?', params + [username or '', n])


def row_continue(model, conn, where, params, username, watch_history, n):
    """In-progress videos (watch history), newest first, within the scope."""
    if watch_history is None or not username:
        return []
    items = watch_history.get_continue_watching(username, 400)
    paths = [i['path'] for i in items]
    if not paths:
        return []
    ok = set()
    for k in range(0, len(paths), 400):
        chunk = paths[k:k + 400]
        ok |= set(_paths(conn, _base(where) + f' AND fv.path IN ({",".join("?" * len(chunk))})', params + chunk))
    return [p for p in paths if p in ok][:n]


def _ids(conn, field, video_id):
    from ext_db import _junction_fk
    fk = _junction_fk(conn, field)
    if not fk:
        return [], None
    return [r[0] for r in conn.execute(f'SELECT {fk} FROM video_{field}_junction WHERE video_id = ?', (video_id,))], fk


def because_seeds(conn, where, params, username, k=BECAUSE_SEEDS):
    """The last videos you really played (counted plays), within the scope."""
    return conn.execute('SELECT v.video_id, fv.path, v.title FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                        'JOIN watch_stats ws ON ws.video_id = v.video_id AND ws.username = ? '
                        f'WHERE {where} AND ws.plays > 0 ORDER BY ws.last_played DESC LIMIT ?',
                        [username or ''] + params + [k]).fetchall()


def row_because(conn, where, params, username, seed_vid, n):
    """Unwatched videos scored against one seed: cast +3 (Male Cast +1), same site +2, tags +1 each (max 3)."""
    cast_ids, cfk = _ids(conn, 'cast', seed_vid)
    site_ids, sfk = _ids(conn, 'site', seed_vid)
    tag_ids, tfk = _ids(conn, 'tags', seed_vid)
    if tag_ids:
        tag_ids = [r[0] for r in conn.execute(f'SELECT {tfk} FROM video_tags_junction WHERE {tfk} IN ({",".join("?" * len(tag_ids))}) '
                                              f'GROUP BY {tfk} HAVING COUNT(*) <= ?', tag_ids + [COMMON_TAG])]
    try:
        import ext_db
        men = {m.lower() for m in ext_db.male_cast_for_video(seed_vid)}
    except Exception:
        men = set()
    male_ids = set()
    if men and cast_ids:
        male_ids = {r[0] for r in conn.execute(f'SELECT id, value FROM video_cast WHERE id IN ({",".join("?" * len(cast_ids))})',
                                               cast_ids) if (r[1] or '').lower() in men}
    parts, p = [], []

    def add(sql, ids, weight):
        if ids:
            parts.append(sql.format(w=weight, ph=','.join('?' * len(ids))))
            p.extend(ids)
    add('SELECT video_id, {w} AS w FROM video_cast_junction WHERE ' + f'{cfk}' + ' IN ({ph})', [i for i in cast_ids if i not in male_ids], 3)
    add('SELECT video_id, {w} AS w FROM video_cast_junction WHERE ' + f'{cfk}' + ' IN ({ph})', list(male_ids), 1)
    add('SELECT video_id, {w} AS w FROM video_site_junction WHERE ' + f'{sfk}' + ' IN ({ph})', site_ids, 2)
    add('SELECT video_id, MIN(COUNT(*), 3) * {w} AS w FROM video_tags_junction WHERE ' + f'{tfk}' + ' IN ({ph}) GROUP BY video_id', tag_ids, 1)
    if not parts:
        return []
    sql = (f'SELECT fv.path FROM (SELECT video_id, SUM(w) AS score FROM ({" UNION ALL ".join(parts)}) GROUP BY video_id) s '
           f'JOIN videos v ON v.video_id = s.video_id JOIN file_versions fv ON fv.video_id = v.video_id '
           f'WHERE {where} AND v.video_id != ? AND s.score >= 3 AND {_unwatched()} '
           f'ORDER BY s.score DESC, {library_stats.arrival_sql("fv")} DESC LIMIT ?')
    return _paths(conn, sql, p + params + [seed_vid, username or '', n])


def build(model, username, library=None, watch_history=None, collections_rows=None, n=ROW_SIZE):
    """-> {'rows': [{key, title, items, see_all}], 'settings'}"""
    import display_filter
    conn = model.db.get_connection()
    library_stats.ensure_schema(conn)
    key = (username, library or 'Home', display_filter.cache_key(conn), n)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    settings = get_settings(conn)
    where, params = scope_sql(model, conn, library)
    rows = []
    mk = lambda p, extra=None: card(model, p, username, watch_history, extra)

    def add(key_, title, paths, see_all=None):
        items = [c for c in (mk(p) for p in paths) if c]
        if items:
            rows.append({'key': key_, 'title': title, 'items': items, 'see_all': see_all})

    for k in settings['order']:
        if k in settings['hidden']:
            continue
        if k == 'continue':
            add(k, ROWS[k], row_continue(model, conn, where, params, username, watch_history, n))
        elif k == 'recently_added':
            add(k, ROWS[k], row_recently_added(conn, where, params, username, n), {'sort': 'Recently Added'})
        elif k == 'recently_watched':
            add(k, ROWS[k], row_recently_watched(conn, where, params, username, n), {'sort': 'Last Played'})
        elif k == 'most_played':
            add(k, ROWS[k], row_most_played(conn, where, params, username, n), {'sort': 'Most Played'})
        elif k == 'favorites':
            add(k, ROWS[k], row_favorites(conn, where, params, username, n), {'favorites': True})
        elif k == 'rediscover':
            add(k, ROWS[k], row_rediscover(conn, where, params, username, n), {'sort': 'Random'})
        elif k == 'because':
            for vid, path, title in because_seeds(conn, where, params, username):
                label = (title or '').strip() or os.path.splitext(os.path.basename(path))[0]
                add(f'because:{vid}', f'Because you watched {label}', row_because(conn, where, params, username, vid, n))
        elif k == 'collections' and collections_rows:
            for r in collections_rows(conn, library, n, model):
                add(f"collection:{r['id']}", r['title'], r['paths'], {'collection': r['id']})
    out = {'rows': rows, 'settings': settings, 'titles': ROWS}
    with _cache_lock:
        _cache[key] = (time.time(), out)
    return out
