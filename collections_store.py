# This file is: ./collections_store.py
"""
Collections (roadmap #9), stored in the playlists table next to playlists:
  kind 'collection' - you pick the videos (playlist_items, like a playlist)
  kind 'smart'      - saved filters: the /api/videos parameters as JSON (rules); the items
                      are whatever those filters match right now, exactly like the saved view
Plus description, cover (cover_video_id), sort (manual ones) and show_on_home.
Playlists (kind 'playlist') are unaffected: the playlist routes only list kind 'playlist'.

No FastAPI imports (the /api/videos function is called for smart collections).
"""
import json

import home
import library_stats

KINDS = ('collection', 'smart')
RULE_KEYS = ('library', 'query', 'tags', 'tags_exclude', 'sites', 'sites_exclude', 'favorites_only', 'length_filter',
             'sort_by', 'desc', 'hide_zero_length')
MANUAL_SORTS = ('position', 'Recently Added', 'Title', 'Most Played', 'Last Played')


class CollectionError(Exception):
    pass


class _NoAuth:                     # smart rows built outside a request (Home): no per-user watch progress
    cookies = {}


def clean_rules(rules):
    r = {k: v for k, v in (rules or {}).items() if k in RULE_KEYS and v not in (None, '')}
    r.setdefault('library', 'All Videos')
    r['favorites_only'] = bool(r.get('favorites_only'))
    r['desc'] = bool(r.get('desc', True))
    r.setdefault('sort_by', 'Recently Added')
    return r


def _row(r):
    return {'id': r[0], 'name': r[1], 'kind': r[2], 'rules': json.loads(r[3]) if r[3] else None, 'sort': r[4] or 'position',
            'description': r[5] or '', 'cover_video_id': r[6], 'show_on_home': bool(r[7]), 'updated_at': r[8]}


def get(conn, cid):
    r = conn.execute('SELECT id, name, kind, rules, sort, description, cover_video_id, show_on_home, updated_at '
                     "FROM playlists WHERE id = ? AND kind IN ('collection', 'smart')", (cid,)).fetchone()
    if not r:
        raise CollectionError('Collection not found')
    return _row(r)


def all_collections(conn):
    return [_row(r) for r in conn.execute('SELECT id, name, kind, rules, sort, description, cover_video_id, show_on_home, updated_at '
                                          "FROM playlists WHERE kind IN ('collection', 'smart') ORDER BY name COLLATE NOCASE")]


def _name_free(conn, name, exclude=None):
    name = (name or '').strip()
    if not name:
        raise CollectionError('Give the collection a name')
    row = conn.execute('SELECT kind FROM playlists WHERE name = ? AND id != ?', (name, exclude or -1)).fetchone()
    if row:
        raise CollectionError(f'"{name}" is already used by a {"playlist" if (row[0] or "playlist") == "playlist" else "collection"}')
    return name


def create(conn, name, kind='collection', rules=None, description='', video_ids=None):
    if kind not in KINDS:
        raise CollectionError(f'Unknown kind: {kind}')
    name = _name_free(conn, name)
    cid = conn.execute('INSERT INTO playlists (name, is_watchlist, kind, rules, description, sort) VALUES (?, 0, ?, ?, ?, ?)',
                       (name, kind, json.dumps(clean_rules(rules)) if kind == 'smart' else None, description or '',
                        None if kind == 'smart' else 'position')).lastrowid
    conn.commit()
    if video_ids and kind == 'collection':
        add(conn, cid, video_ids)
    home.invalidate()
    return cid


def update(conn, cid, changes):
    c = get(conn, cid)
    sets, params = [], []
    if 'name' in changes:
        sets.append('name = ?'); params.append(_name_free(conn, changes['name'], cid))
    if 'description' in changes:
        sets.append('description = ?'); params.append(changes['description'] or '')
    if 'cover_video_id' in changes:
        sets.append('cover_video_id = ?'); params.append(changes['cover_video_id'] or None)
    if 'show_on_home' in changes:
        sets.append('show_on_home = ?'); params.append(1 if changes['show_on_home'] else 0)
    if 'rules' in changes:
        if c['kind'] != 'smart':
            raise CollectionError('Only smart collections have rules')
        sets.append('rules = ?'); params.append(json.dumps(clean_rules(changes['rules'])))
    if 'sort' in changes:
        if changes['sort'] not in MANUAL_SORTS:
            raise CollectionError(f'Unknown sort: {changes["sort"]}')
        sets.append('sort = ?'); params.append(changes['sort'])
    if sets:
        conn.execute(f'UPDATE playlists SET {", ".join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?', params + [cid])
        conn.commit()
        home.invalidate()
    return get(conn, cid)


def delete(conn, cid):
    get(conn, cid)
    conn.execute('DELETE FROM playlist_items WHERE playlist_id = ?', (cid,))
    conn.execute('DELETE FROM playlists WHERE id = ?', (cid,))
    conn.commit()
    home.invalidate()


def add(conn, cid, video_ids):
    if get(conn, cid)['kind'] != 'collection':
        raise CollectionError('Smart collections fill themselves - change their filters instead')
    pos = (conn.execute('SELECT MAX(position) FROM playlist_items WHERE playlist_id = ?', (cid,)).fetchone()[0] or 0) + 1
    n = 0
    for vid in video_ids or []:
        if conn.execute('INSERT OR IGNORE INTO playlist_items (playlist_id, video_id, position) VALUES (?, ?, ?)',
                        (cid, vid, pos)).rowcount:
            pos += 1
            n += 1
    conn.execute('UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (cid,))
    conn.commit()
    home.invalidate()
    return n


def remove(conn, cid, video_ids):
    get(conn, cid)
    ph = ','.join('?' * len(video_ids or [])) or "''"
    n = conn.execute(f'DELETE FROM playlist_items WHERE playlist_id = ? AND video_id IN ({ph})', [cid] + list(video_ids or [])).rowcount
    conn.commit()
    home.invalidate()
    return n


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------
def _smart_query(model, request, rules, page, limit, seed=0):
    import routes.videos as rv
    r = clean_rules(rules)
    args = dict(query=r.get('query') or '', sort_by=r['sort_by'], desc=r['desc'], library=r['library'],
                tags=r.get('tags'), tags_exclude=r.get('tags_exclude'), sites=r.get('sites'), sites_exclude=r.get('sites_exclude'),
                tag_filter=None, tag_exclude=None, favorites_only=r['favorites_only'], page=page, limit=limit, folder_mode=False,
                current_folder=None, hide_zero_length=r.get('hide_zero_length'), length_filter=r.get('length_filter'), seed=seed,
                watch_history=rv._watch_history)
    return rv.get_videos(request or _NoAuth(), model=model, **args)


def _manual_paths(model, conn, c, library=None, username=None):
    """Paths of a manual collection, in its sort, within the library scope and display filter."""
    where, params = home.scope_sql(model, conn, library)
    rows = [{'video_id': r[0], 'path': r[1], 'position': r[2], 'title': (r[3] or '').lower()} for r in conn.execute(
        f'SELECT v.video_id, fv.path, pi.position, COALESCE(NULLIF(v.title, \'\'), fv.filename) FROM playlist_items pi '
        f'JOIN videos v ON v.video_id = pi.video_id JOIN file_versions fv ON fv.video_id = v.video_id '
        f'WHERE pi.playlist_id = ? AND {where}', [c['id']] + params)]
    sort = c['sort'] or 'position'
    if sort == 'position':
        rows.sort(key=lambda r: r['position'] or 0)
    elif sort == 'Title':
        rows.sort(key=lambda r: r['title'])
    else:
        library_stats.special_sort(conn, rows, library_stats.sort_field(sort), username, desc=True)
    return [r['path'] for r in rows]


def items(model, conn, cid, request=None, username=None, watch_history=None, page=1, limit=100, seed=0):
    c = get(conn, cid)
    if c['kind'] == 'smart':
        d = _smart_query(model, request, c['rules'], page, limit, seed)
        return {'collection': c, 'items': d.get('items', []), 'total': d.get('total', 0)}
    paths = _manual_paths(model, conn, c, None, username)
    start = (max(1, page) - 1) * limit
    cards = [x for x in (home.card(model, p, username, watch_history) for p in paths[start:start + limit]) if x]
    return {'collection': c, 'items': cards, 'total': len(paths)}


def _cover(model, conn, c, first_path=None):
    from helpers import get_thumbnail_path
    from media_handler import MediaHandler
    path = None
    if c['cover_video_id']:
        r = conn.execute('SELECT path FROM file_versions WHERE video_id = ? AND is_preferred = 1 AND deleted_at IS NULL',
                         (c['cover_video_id'],)).fetchone()
        path = r[0] if r else None
    path = path or first_path
    if not path:
        return None
    r = conn.execute('SELECT video_id, cache_id FROM file_versions WHERE path = ?', (path,)).fetchone()
    return get_thumbnail_path(r[1] or MediaHandler._get_cache_key(path, r[0], r[1])) if r else None


def _in_library(model, rules_library, library):
    """Does a smart collection's library belong under this library / group tab?"""
    if library in home.EVERYTHING:
        return True
    if rules_library == library:
        return True
    groups = getattr(model, 'library_groups', None) or {}
    return library in groups and rules_library in (model.get_libraries_in_group(library) or [])


def listing(model, conn, library=None, request=None, username=None):
    """Collections for a library / group tab (all of them for All Videos / Home), with count and cover."""
    out = []
    for c in all_collections(conn):
        if c['kind'] == 'smart':
            if not _in_library(model, (c['rules'] or {}).get('library', 'All Videos'), library):
                continue
            try:
                d = _smart_query(model, request, c['rules'], 1, 1)
                count, first = d.get('total', 0), (d.get('items') or [{}])[0].get('path')
            except Exception:
                count, first = 0, None
        else:
            paths = _manual_paths(model, conn, c, library, username)
            if library not in home.EVERYTHING and not paths:
                continue
            count, first = len(paths), (paths[0] if paths else None)
        out.append(dict(c, count=count, cover=_cover(model, conn, c, first)))
    return out


def home_rows(conn, library, n, model=None):
    """Rows for collections marked 'show on Home' (home.build)."""
    import routes.videos as rv
    model = model or rv._model
    rows = []
    for c in all_collections(conn):
        if not c['show_on_home']:
            continue
        if c['kind'] == 'smart':
            if not _in_library(model, (c['rules'] or {}).get('library', 'All Videos'), library):
                continue
            try:
                paths = [i['path'] for i in _smart_query(model, None, c['rules'], 1, n).get('items', [])]
            except Exception:
                paths = []
        else:
            paths = _manual_paths(model, conn, c, library)[:n]
        if paths:
            rows.append({'id': c['id'], 'title': c['name'], 'paths': paths})
    return rows
