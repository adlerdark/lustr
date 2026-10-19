# This file is: ./movies.py
"""
Full movies: which movie a video belongs to (a scene, a part, or the whole movie in one file),
and each movie's own details, cover and ThePornDB link.

movies(id, title, title_key, year, studio, director, description, cover, cover_mime, cover_source,
       cover_video_id, ext_source, ext_id, ext_url, ext_raw_json, created_at, updated_at)
  title_key: lowercase letters and digits only, so "Jane's Diary 5" and "jane-s-diary-5" are one movie.
  The movie's own details are never copied onto its scenes.
video_movie(video_id, movie_id, scene, part, whole, source)
  scene=N: a scene · scene=N + part=M: a long scene split in files · part=N: a DVD split in files ·
  whole=1: the whole movie in one file. source: manual | title | ext.

A movie without videos is removed, unless it's linked to ThePornDB or has a cover you gave it.
Merges move the row (routes/videos.py _merge_move_rows), splits copy it; rows of deleted videos
are dropped by prune().

No FastAPI imports.
"""
import json
import re
import unicodedata
from collections import Counter

_schema_ready = set()
_KEEP = object()
UPDATE_KEYS = ('movie', 'movie_scene', 'movie_part', 'movie_whole')


def ensure_schema(conn):
    if id(conn) in _schema_ready:
        return
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('movies', 'video_movie', 'idx_video_movie_movie')")}
    if len(have) == 3:                       # already there: no commit (it could split a caller's transaction)
        _schema_ready.add(id(conn))
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        title_key TEXT NOT NULL UNIQUE,
        year TEXT, studio TEXT, director TEXT, description TEXT,
        cover BLOB, cover_mime TEXT, cover_source TEXT,          -- 'user' | 'ext'
        cover_video_id TEXT,
        ext_source TEXT, ext_id TEXT, ext_url TEXT, ext_raw_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS video_movie (
        video_id TEXT PRIMARY KEY,
        movie_id INTEGER NOT NULL,
        scene INTEGER, part INTEGER,
        whole INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual')''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_video_movie_movie ON video_movie(movie_id)')
    conn.commit()
    _schema_ready.add(id(conn))


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
def _fold(s):
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c))


def title_key(title):
    """'Night Shift 02' and 'night-shift-2' -> 'nightshift2'."""
    t = re.sub(r'(?<!\d)0+(?=\d)', '', _fold(title).lower())
    return re.sub(r'[^a-z0-9]+', '', t)


def clean_title(title):
    t = re.sub(r'\s+', ' ', (title or '').strip())
    return t.strip(' -,:;_.(').strip()


def _nice(t):
    """ALL CAPS or all lowercase -> Title Case; mixed case is kept as typed."""
    if t and (t == t.lower() or t == t.upper()):
        return ' '.join(w[:1].upper() + w[1:].lower() for w in t.split(' '))
    return t


def _tidy(name):
    """'JaneDoeGetsDP' / 'office-party' / 'some_movie' (no spaces) -> words, then _nice."""
    n = clean_title(name)
    if ' ' not in n and re.search(r'[_-]|[a-z][A-Z]', n):
        n = _camel_words(n.replace('_', ' '))
    return _nice(n)


def best_spelling(names):
    """The spelling to show for a group: mixed case first, then the most common."""
    counts = Counter(n for n in names if n)
    if not counts:
        return ''
    return max(counts, key=lambda n: (n != n.lower() and n != n.upper(), counts[n], -len(n)))


# ---------------------------------------------------------------------------
# reading a movie out of a title / filename
# ---------------------------------------------------------------------------
_NOISE = re.compile(r'^(?:\d{3,4}p|\d{3,5}|[hx]\.?26[45]|hevc|4k|uhd|[fsu]?hd|sd|xxx|mp4|web(?:-?dl)?|remastered|'
                    r'(?:\d+(?:\.\d+)?)(?:mb|gb|kbps|mbps))$', re.I)
_SCENE = re.compile(r'^(?P<m>.*?)[\s,._:-]*\b(?:scene|sc)[\s._#:-]*(?P<n>\d{1,2})(?!\d)(?P<rest>.*)$', re.I)
_PART = re.compile(r'^(?P<m>.*?)[\s,._:-]*\b(?:part|pt)\.?[\s._#-]*(?P<n>\d{1,2})(?!\d)(?P<rest>.*)$', re.I)
_DISC = re.compile(r'^(?P<m>.*?)[\s,._:(-]*\b(?:disc|disk|cd|dvd)[\s._#-]*(?P<n>\d)(?!\d)(?P<rest>.*)$', re.I)
_TAIL_S = re.compile(r'^(?P<m>.*\S)[\s._-]+s(?P<n>\d{1,2})$', re.I)
_FALCON = re.compile(r'^(?P<m>[A-Za-z0-9&-]{3,}?)_s(?P<n>\d{2})_', re.I)


def _rest_kind(rest):
    """What follows "Scene N": '' (nothing), 'noise' (resolution, bitrate...), ('part', n) or 'words'."""
    r = rest.strip(' -,.:;_)]')
    if not r:
        return ''
    m = re.match(r'^(?:part|pt)\.?[\s._#-]*(\d{1,2})(?!\d)(.*)$', r, re.I)
    if m:
        return ('part', int(m.group(1)), _rest_kind(m.group(2)))
    if re.fullmatch(r'\(.*\)', r):
        return 'paren'
    words = [w for w in re.split(r'[\s._-]+', r) if w]
    return 'noise' if words and all(_NOISE.match(w) for w in words) else 'words'


def _valid_movie(m):
    k = title_key(m)
    return len(k) >= 3 and not k.isdigit()


def parse_movie(title):
    """'Summer Road Trip 13 Scene 1' -> {'movie': 'Summer Road Trip 13', 'scene': 1, 'part': None, 'sure': True}, or None.
    sure=False for the forms that are often something else (Part N, S02, Disc N, extra words after)."""
    t = re.sub(r'\s+', ' ', (title or '').strip())
    if not t:
        return None
    m = _SCENE.match(t)
    if m and _valid_movie(m.group('m')):
        kind = _rest_kind(m.group('rest'))
        part = kind[1] if isinstance(kind, tuple) else None
        tail = kind[2] if isinstance(kind, tuple) else kind
        return {'movie': clean_title(m.group('m')), 'scene': int(m.group('n')), 'part': part,
                'sure': tail in ('',) and not re.search(r'\d{3,4}p\b', m.group('m'), re.I)}
    for rx in (_DISC, _PART):
        m = rx.match(t)
        if m and _valid_movie(m.group('m')) and _rest_kind(m.group('rest')) in ('', 'noise', 'paren'):
            return {'movie': clean_title(m.group('m')), 'scene': None, 'part': int(m.group('n')), 'sure': False}
    m = _TAIL_S.match(t)
    if m and _valid_movie(m.group('m')):
        return {'movie': clean_title(m.group('m')), 'scene': int(m.group('n')), 'part': None, 'sure': False}
    return None


def _camel_words(s):
    s = s.replace('-', ' ')
    s = re.sub(r'(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def filename_text(filename_base):
    """'Jane Doe - Some_Movie_2_Scene_3' -> 'Some Movie 2 Scene 3' (the part after the cast)."""
    s = filename_base or ''
    if ' - ' in s:
        s = s.split(' - ', 1)[1]
    s = re.sub(r'\.(?:\d{3,4}p|mp4|mkv|wmv|avi)$', '', s, flags=re.I)
    s = re.sub(r'[_.]+', ' ', s)
    s = re.sub(r'(?<=\w)-(?=\w)', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def parse_video(title, filename_base):
    """The movie for one video, from its title, else its filename (always 'not sure' then)."""
    p = parse_movie(title)
    if p:
        return dict(p, movie=_tidy(p['movie']), how='title')
    m = _FALCON.match(filename_base.split(' - ', 1)[-1] if filename_base else '')
    if m:
        return {'movie': _camel_words(m.group('m')), 'scene': int(m.group('n')), 'part': None, 'sure': False, 'how': 'filename'}
    p = parse_movie(filename_text(filename_base))
    if p:
        return dict(p, movie=_nice(p['movie']), sure=False, how='filename')
    return None


# ---------------------------------------------------------------------------
# one video's movie
# ---------------------------------------------------------------------------
def _int_or_none(v):
    if v is None or v is _KEEP:
        return v
    try:
        n = int(str(v).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def find_movie(conn, title):
    ensure_schema(conn)
    r = conn.execute('SELECT id FROM movies WHERE title_key = ?', (title_key(title),)).fetchone()
    return r[0] if r else None


def get_or_create(conn, title):
    title = clean_title(title)
    if not title_key(title):
        raise ValueError('A movie needs a name')
    mid = find_movie(conn, title)
    if mid:
        return mid
    return conn.execute('INSERT INTO movies (title, title_key) VALUES (?, ?)', (title, title_key(title))).lastrowid


def set_video_movie(conn, video_id, title=_KEEP, scene=_KEEP, part=_KEEP, whole=_KEEP, source='manual', commit=True):
    """Put a video in a movie (found by name, or made), change its scene / part / whole, or take it
    out (title ''). _KEEP leaves a value as it is. Returns the movie id (None when out)."""
    ensure_schema(conn)
    old = conn.execute('SELECT movie_id, scene, part, whole FROM video_movie WHERE video_id = ?', (video_id,)).fetchone()
    if title is not _KEEP and not title_key(title or ''):
        conn.execute('DELETE FROM video_movie WHERE video_id = ?', (video_id,))
        if old:
            drop_empty(conn, [old[0]])
        if commit:
            conn.commit()
        return None
    if title is _KEEP:
        if not old:
            return None                                     # numbers alone need a movie
        mid = old[0]
    else:
        mid = get_or_create(conn, title)
    scene, part = _int_or_none(scene), _int_or_none(part)
    vals = (scene if scene is not _KEEP else (old[1] if old else None),
            part if part is not _KEEP else (old[2] if old else None),
            (1 if whole and str(whole) not in ('0', 'false', '') else 0) if whole is not _KEEP else (old[3] if old else 0))
    conn.execute('INSERT OR REPLACE INTO video_movie (video_id, movie_id, scene, part, whole, source) VALUES (?, ?, ?, ?, ?, ?)',
                 (video_id, mid) + vals + (source,))
    if old and old[0] != mid:
        drop_empty(conn, [old[0]])
    if commit:
        conn.commit()
    return mid


def apply_updates(conn, video_id, updates):
    """Edit Metadata: the movie / movie_scene / movie_part / movie_whole keys that were changed."""
    upd = {k: v for k, v in (updates or {}).items() if k in UPDATE_KEYS}
    if not upd:
        return
    set_video_movie(conn, video_id, title=upd.get('movie', _KEEP), scene=upd.get('movie_scene', _KEEP),
                    part=upd.get('movie_part', _KEEP), whole=upd.get('movie_whole', _KEEP), commit=False)


def copy_movie(conn, from_video_id, to_video_id):
    """Split: the new video is in the same movie, with the same numbers."""
    ensure_schema(conn)
    conn.execute('INSERT OR IGNORE INTO video_movie (video_id, movie_id, scene, part, whole, source) '
                 'SELECT ?, movie_id, scene, part, whole, source FROM video_movie WHERE video_id = ?', (to_video_id, from_video_id))
    conn.commit()


def drop_empty(conn, movie_ids=None):
    """Remove movies without videos - not ones linked to ThePornDB or with your own cover."""
    keep = "ext_id IS NULL AND COALESCE(cover_source, '') != 'user'"
    empty = 'id NOT IN (SELECT vm.movie_id FROM video_movie vm JOIN videos v ON v.video_id = vm.video_id)'
    if movie_ids is None:
        return conn.execute(f'DELETE FROM movies WHERE {keep} AND {empty}').rowcount
    ids = [m for m in movie_ids if m]
    if not ids:
        return 0
    return conn.execute(f'DELETE FROM movies WHERE id IN ({",".join("?" * len(ids))}) AND {keep} AND {empty}', ids).rowcount


def prune(conn):
    """Rows of videos that are gone, then the movies left empty. -> (rows, movies) removed."""
    ensure_schema(conn)
    n = conn.execute('DELETE FROM video_movie WHERE video_id NOT IN (SELECT video_id FROM videos)').rowcount
    m = drop_empty(conn)
    conn.commit()
    return n, m


def info_map(conn, video_ids):
    """{video_id: (movie_id, title, scene, part, whole)}. No schema check (it commits): the tables
    are made at start-up, and add_movie_info treats a missing table as no movies."""
    ids, out = list({v for v in video_ids if v}), {}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in conn.execute(f'SELECT vm.video_id, m.id, m.title, vm.scene, vm.part, vm.whole FROM video_movie vm '
                              f'JOIN movies m ON m.id = vm.movie_id WHERE vm.video_id IN ({",".join("?" * len(chunk))})', chunk):
            out[r[0]] = tuple(r[1:])
    return out


def add_movie_info(conn, items):
    """movie / movie_id / movie_scene / movie_part / movie_whole on video dicts (strings, '' when none,
    so Edit Metadata can compare them like the other text fields)."""
    items = [i for i in items if isinstance(i, dict)]
    try:
        info = info_map(conn, [i.get('video_id') for i in items])
    except Exception:
        info = {}
    for i in items:
        mid, title, scene, part, whole = info.get(i.get('video_id'), (None, '', None, None, 0))
        i.update({'movie': title or '', 'movie_id': mid, 'movie_scene': str(scene) if scene else '',
                  'movie_part': str(part) if part else '', 'movie_whole': '1' if whole else ''})
    return items


def sort_label(item):
    """Library 'Movie' column: sorts by movie, then whole files, scene, part."""
    t = item.get('movie') or ''
    if not t:
        return ''
    num = lambda v: int(v) if str(v or '').isdigit() else 0
    return f"{t}\x01{0 if item.get('movie_whole') else 1}{num(item.get('movie_scene')):04d}{num(item.get('movie_part')):04d}"


def filter_sql(values, col='v.video_id'):
    """movie:"Name" / movie:Name* / movie:yes / movie:no -> (sql, params)."""
    base = 'SELECT vm.video_id FROM video_movie vm JOIN movies m ON m.id = vm.movie_id'
    ors, params = [], []
    for v in values:
        exact = isinstance(v, tuple)
        v = str(v[1] if exact else v).strip()
        lv = v.lower()
        if lv in ('yes', 'any', 'true'):
            ors.append(f'{col} IN (SELECT video_id FROM video_movie)')
        elif lv in ('no', 'none', 'false'):
            ors.append(f'{col} NOT IN (SELECT video_id FROM video_movie)')
        elif exact and '*' not in v and '%' not in v:
            ors.append(f'{col} IN ({base} WHERE m.title_key = ?)')
            params.append(title_key(v))
        else:
            pat = v.replace('*', '%')
            ors.append(f'{col} IN ({base} WHERE m.title LIKE ?)')
            params.append(pat if '%' in pat else f'%{pat}%')
    return ('(' + ' OR '.join(ors) + ')', params) if ors else ('', [])


def autocomplete(conn, q, limit=15):
    ensure_schema(conn)
    q = (q or '').strip()
    if not q:
        return []
    return [r[0] for r in conn.execute('SELECT title FROM movies WHERE title LIKE ? ORDER BY (title LIKE ?) DESC, title LIMIT ?',
                                       (f'%{q}%', f'{q}%', limit))]


# ---------------------------------------------------------------------------
# the movie itself
# ---------------------------------------------------------------------------
_FIELDS = ('title', 'year', 'studio', 'director', 'description', 'cover_video_id')


def get(conn, mid):
    ensure_schema(conn)
    cur = conn.execute('SELECT id, title, year, studio, director, description, cover IS NOT NULL, cover_source, cover_video_id, '
                       'ext_source, ext_id, ext_url, ext_raw_json, created_at, updated_at FROM movies WHERE id = ?', (mid,))
    r = cur.fetchone()
    if not r:
        raise ValueError('That movie no longer exists')
    d = dict(zip([c[0] for c in cur.description], r))
    d['has_cover'] = bool(d.pop('cover IS NOT NULL'))
    raw = d.pop('ext_raw_json')
    d['ext_scenes'] = _ext_scenes(raw)
    return d


def _ext_scenes(raw):
    try:
        x = json.loads(raw) if raw else {}
    except ValueError:
        return []
    out = []
    for i, sc in enumerate(x.get('scenes') or []):
        if not isinstance(sc, dict):
            continue
        perfs = []
        for p in sc.get('performers') or []:
            if isinstance(p, dict):
                perfs.append(((p.get('parent') or {}).get('name')) or p.get('name') or '')
        out.append({'n': i + 1, 'title': sc.get('title') or '', 'duration': sc.get('duration'),
                    'performers': [p for p in perfs if p]})
    return out


def update(conn, mid, changes, merge=False):
    """Change a movie's own details. Renaming onto another movie's name -> {'conflict': {...}}
    unless merge=True, which moves this movie's videos into that one."""
    ensure_schema(conn)
    get(conn, mid)
    sets, params = [], []
    for k in _FIELDS:
        if k not in changes:
            continue
        v = changes[k]
        if k == 'title':
            v = clean_title(v)
            if not title_key(v):
                raise ValueError('A movie needs a name')
            other = conn.execute('SELECT id, title FROM movies WHERE title_key = ? AND id != ?', (title_key(v), mid)).fetchone()
            if other:
                if not merge:
                    return {'conflict': {'id': other[0], 'title': other[1]}}
                merge_into(conn, [mid], other[0])
                return {'merged_into': other[0]}
            sets += ['title = ?', 'title_key = ?']
            params += [v, title_key(v)]
        else:
            sets.append(f'{k} = ?')
            params.append((str(v).strip() or None) if v is not None else None)
    if sets:
        conn.execute(f"UPDATE movies SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", params + [mid])
        conn.commit()
    return {'movie': get(conn, mid)}


def merge_into(conn, source_ids, target_id):
    """Move the videos of these movies into the target; empty details are filled from them."""
    ensure_schema(conn)
    src = [s for s in source_ids if s and s != target_id]
    if not src:
        return 0
    cols = ('year', 'studio', 'director', 'description', 'ext_source', 'ext_id', 'ext_url', 'ext_raw_json')
    for s in src:
        conn.execute(f"UPDATE movies SET {', '.join(f'{c} = COALESCE({c}, (SELECT {c} FROM movies WHERE id = ?))' for c in cols)} "
                     f'WHERE id = ?', [s] * len(cols) + [target_id])
        conn.execute('UPDATE movies SET cover = (SELECT cover FROM movies WHERE id = ?), cover_mime = (SELECT cover_mime FROM movies WHERE id = ?), '
                     'cover_source = (SELECT cover_source FROM movies WHERE id = ?) WHERE id = ? AND cover IS NULL', (s, s, s, target_id))
    ph = ','.join('?' * len(src))
    n = conn.execute(f'UPDATE video_movie SET movie_id = ? WHERE movie_id IN ({ph})', [target_id] + src).rowcount
    conn.execute(f'DELETE FROM movies WHERE id IN ({ph})', src)
    conn.execute('UPDATE movies SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (target_id,))
    conn.commit()
    return n


def delete(conn, mid):
    """Delete a movie; its videos stay, just without a movie."""
    ensure_schema(conn)
    conn.execute('DELETE FROM video_movie WHERE movie_id = ?', (mid,))
    conn.execute('DELETE FROM movies WHERE id = ?', (mid,))
    conn.commit()


def set_cover(conn, mid, data, mime, source='user'):
    ensure_schema(conn)
    conn.execute('UPDATE movies SET cover = ?, cover_mime = ?, cover_source = ?, cover_video_id = NULL, '
                 'updated_at = CURRENT_TIMESTAMP WHERE id = ?', (data, mime, source if data else None, mid))
    conn.commit()


def cover(conn, mid):
    ensure_schema(conn)
    r = conn.execute('SELECT cover, cover_mime FROM movies WHERE id = ?', (mid,)).fetchone()
    return (r[0], r[1] or 'image/jpeg') if r and r[0] else None


# ---------------------------------------------------------------------------
# lists (Movies tab) and one movie's page
# ---------------------------------------------------------------------------
def _thumb(cache_id, path, video_id):
    from helpers import get_thumbnail_path
    from media_handler import MediaHandler
    return get_thumbnail_path(cache_id or MediaHandler._get_cache_key(path, video_id, cache_id))


def _order(scene, part, whole, title):
    return (0 if whole else 1, scene if scene else 999, part if part else 0, (title or '').lower())


def listing(model, conn, library=None, q='', sort='title', page=1, limit=120):
    """Movies with at least one video in this library / group (every movie for All Videos)."""
    import home
    import library_stats
    ensure_schema(conn)
    where, params = home.scope_sql(model, conn, library)
    movies = {}
    for r in conn.execute(
            f'SELECT m.id, m.title, m.year, m.studio, m.ext_id, m.cover IS NOT NULL, m.cover_video_id, m.updated_at, '
            f'vm.video_id, vm.scene, vm.part, vm.whole, v.title, fv.path, fv.cache_id, {library_stats.arrival_sql("fv")} '
            f'FROM movies m JOIN video_movie vm ON vm.movie_id = m.id JOIN videos v ON v.video_id = vm.video_id '
            f'JOIN file_versions fv ON fv.video_id = v.video_id WHERE {where}', params):
        m = movies.setdefault(r[0], {'id': r[0], 'title': r[1], 'year': r[2] or '', 'studio': r[3] or '', 'linked': bool(r[4]),
                                     'has_cover': bool(r[5]), 'cover_video_id': r[6], 'updated_at': r[7], 'count': 0,
                                     'added': '', '_first': None, '_cover': None})
        m['count'] += 1
        m['added'] = max(m['added'], r[15] or '')
        key = _order(r[9], r[10], r[11], r[12])
        if m['_first'] is None or key < m['_first'][0]:
            m['_first'] = (key, r[13], r[14], r[8])
        if r[8] == r[6]:
            m['_cover'] = (r[13], r[14], r[8])
    if library in home.EVERYTHING:                     # kept movies with none of your videos (yet)
        for r in conn.execute('SELECT id, title, year, studio, ext_id, cover IS NOT NULL, updated_at FROM movies '
                              'WHERE id NOT IN (SELECT movie_id FROM video_movie vm JOIN videos v ON v.video_id = vm.video_id)'):
            movies.setdefault(r[0], {'id': r[0], 'title': r[1], 'year': r[2] or '', 'studio': r[3] or '', 'linked': bool(r[4]),
                                     'has_cover': bool(r[5]), 'updated_at': r[6], 'count': 0, 'added': '',
                                     '_first': None, '_cover': None})
    out = list(movies.values())
    ql = title_key(q or '')
    if ql:
        out = [m for m in out if ql in title_key(m['title']) or ql in title_key(m['studio'])]
    s = (sort or 'title').lower()
    out.sort(key=lambda m: _fold(m['title']).lower())
    if s == 'year':
        out.sort(key=lambda m: m['year'] or '0000', reverse=True)
    elif s == 'count':
        out.sort(key=lambda m: m['count'], reverse=True)
    elif s == 'added':
        out.sort(key=lambda m: m['added'], reverse=True)
    total = len(out)
    start = (max(1, page) - 1) * limit
    out = out[start:start + limit]
    for m in out:
        pick = m.pop('_cover') or (m['_first'][1:] if m['_first'] else None)
        m.pop('_first')
        m.pop('cover_video_id', None)
        m['cover'] = (f"/api/movies/{m['id']}/cover?v={m['updated_at'] or ''}" if m['has_cover']
                      else (_thumb(pick[1], pick[0], pick[2]) if pick else None))
    return {'movies': out, 'total': total}


def detail(model, conn, mid, username=None, watch_history=None):
    """A movie, its videos in order (the same records as the Library) and its ThePornDB scene list."""
    import home
    import library_stats
    d = get(conn, mid)
    where, params = home.scope_sql(model, conn, None)
    rows = conn.execute(f'SELECT fv.path, vm.scene, vm.part, vm.whole, v.title, fv.length_seconds, v.video_id FROM video_movie vm '
                        f'JOIN videos v ON v.video_id = vm.video_id JOIN file_versions fv ON fv.video_id = v.video_id '
                        f'WHERE vm.movie_id = ? AND {where}', [mid] + params).fetchall()
    rows = sorted(rows, key=lambda r: _order(r[1], r[2], r[3], r[4]))
    items = [x for x in (home.card(model, r[0], username, watch_history) for r in rows) if x]
    library_stats.add_play_stats(conn, username, items)
    have = {r[1] for r in rows if r[1]}
    for sc in d['ext_scenes']:
        sc['have'] = sc['n'] in have
    d['runtime'] = sum(r[5] or 0 for r in rows)
    d['count'] = len(items)
    pick = next((r for r in rows if r[6] == d['cover_video_id']), rows[0] if rows else None)
    d['cover'] = (f"/api/movies/{mid}/cover?v={d['updated_at'] or ''}" if d['has_cover'] else
                  (next((i['thumb'] for i in items if i['path'] == pick[0]), None) if pick else None))
    if d['ext_source'] == 'tpdb' and d['ext_id'] and not d['ext_url']:
        d['ext_url'] = f"https://theporndb.net/movies/{d['ext_id']}"
    return {'movie': d, 'items': items}


# ---------------------------------------------------------------------------
# Find movies: proposals from titles, filenames and ThePornDB links
# ---------------------------------------------------------------------------
def _ext_proposals(conn, video_ids=None):
    """{video_id: proposal} from ThePornDB links: a movie link has the movie and scene number,
    a scene link may list the movie it's from."""
    out = {}
    try:
        rows = conn.execute("SELECT video_id, ext_kind, ext_movie_title, ext_scene_number, ext_id, ext_raw_json "
                            "FROM video_ext_link WHERE ext_source = 'tpdb'").fetchall()
    except Exception:
        return out
    for vid, kind, mtitle, num, ext_id, raw in rows:
        if video_ids is not None and vid not in video_ids:
            continue
        if kind == 'movie' and mtitle:
            out[vid] = {'movie': clean_title(mtitle), 'scene': num, 'part': None, 'sure': True, 'how': 'ThePornDB',
                        'ext_id': ext_id}
            continue
        try:
            mv = [m for m in (json.loads(raw or '{}').get('movies') or []) if isinstance(m, dict) and m.get('title')]
        except ValueError:
            mv = []
        if mv:
            out[vid] = {'movie': clean_title(mv[0]['title']), 'scene': None, 'part': None, 'sure': True, 'how': 'ThePornDB',
                        'ext_id': mv[0].get('id')}
    return out


def find(model, conn, library=None, whole=False):
    """Groups of videos (not in a movie yet) that look like one movie, for Find Movies to review.
    whole=True: every video of the library is a whole movie unless its title says Part / Disc / Scene."""
    import home
    ensure_schema(conn)
    where, params = home.scope_sql(model, conn, library)
    rows = conn.execute(f'SELECT v.video_id, v.title, v.filename_base, v.library FROM videos v '
                        f'JOIN file_versions fv ON fv.video_id = v.video_id WHERE {where} '
                        f'AND v.video_id NOT IN (SELECT video_id FROM video_movie)', params).fetchall()
    ext = _ext_proposals(conn, {r[0] for r in rows})
    groups = {}
    for vid, title, fbase, lib in rows:
        p = ext.get(vid)
        if p and p['scene'] is None:
            own = parse_video(title, fbase)
            if own and title_key(own['movie']) == title_key(p['movie']):
                p = dict(p, scene=own['scene'], part=own['part'])
        p = p or parse_video(title, fbase)
        if not p and whole:
            name = clean_title(title) or _nice(filename_text(fbase))
            if title_key(name):
                p = {'movie': name, 'scene': None, 'part': None, 'whole': True, 'sure': True, 'how': 'whole'}
        if not p:
            continue
        if whole and p.get('how') != 'whole':
            p = dict(p, sure=True)
        k = title_key(p['movie'])
        g = groups.setdefault(k, {'key': k, 'names': [], 'videos': [], 'ext_id': None})
        g['names'].append(p['movie'])
        if p.get('ext_id') and not g['ext_id']:
            g['ext_id'] = p['ext_id']
        g['videos'].append({'video_id': vid, 'title': title or fbase, 'library': lib, 'scene': p['scene'], 'part': p['part'],
                            'whole': bool(p.get('whole')), 'sure': bool(p['sure']), 'how': p['how'],
                            'source': 'ext' if p['how'] == 'ThePornDB' else 'title'})
    existing = {r[0]: (r[1], r[2]) for r in conn.execute('SELECT title_key, id, title FROM movies')}
    out = []
    for k, g in groups.items():
        vs = g['videos']
        if not whole and not any(x['sure'] or x['part'] or x['scene'] != 1 for x in vs):
            continue                    # only 'Name_s01_...' release names: single scenes, not a movie
        ext_names = [v for v, x in zip(g['names'], g['videos']) if x['how'] == 'ThePornDB']
        title_names = [v for v, x in zip(g['names'], g['videos']) if x['how'] in ('title', 'whole')]
        name = best_spelling(ext_names) or best_spelling(title_names) or best_spelling(g['names'])
        ex = existing.get(k)
        g['videos'].sort(key=lambda x: _order(x['scene'], x['part'], x['whole'], x['title']))
        out.append({'key': k, 'name': ex[1] if ex else name, 'existing_id': ex[0] if ex else None,
                    'sure': any(x['sure'] for x in g['videos']), 'ext_id': g['ext_id'], 'videos': g['videos']})
    out.sort(key=lambda g: _fold(g['name']).lower())
    return out


def apply_found(conn, groups):
    """Put the ticked groups' videos in their movies (one transaction). Videos that got a movie
    meanwhile are left alone. -> (videos, movies made)"""
    ensure_schema(conn)
    n_videos, made = 0, 0
    taken = {r[0] for r in conn.execute('SELECT video_id FROM video_movie')}
    alive = {r[0] for r in conn.execute('SELECT video_id FROM videos')}
    try:
        for g in groups:
            name = clean_title(g.get('name') or '')
            if not title_key(name):
                continue
            existed = find_movie(conn, name)
            mid = existed or get_or_create(conn, name)
            made += 0 if existed else 1
            for v in g.get('videos') or []:
                vid = v.get('video_id')
                if not vid or vid in taken or vid not in alive:
                    continue
                taken.add(vid)
                conn.execute('INSERT INTO video_movie (video_id, movie_id, scene, part, whole, source) VALUES (?, ?, ?, ?, ?, ?)',
                             (vid, mid, _int_or_none(v.get('scene')), _int_or_none(v.get('part')), 1 if v.get('whole') else 0,
                              'ext' if v.get('source') == 'ext' else 'title'))
                n_videos += 1
                if v.get('source') == 'ext':
                    link_from_video(conn, mid, vid, commit=False)
        drop_empty(conn)                 # names whose videos all got a movie meanwhile
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n_videos, made


# ---------------------------------------------------------------------------
# ThePornDB
# ---------------------------------------------------------------------------
def _fill_from_tpdb(conn, mid, raw, ext_id):
    """Link the movie and fill its empty details from a ThePornDB movie record."""
    site = raw.get('site') or {}
    directors = ', '.join(d.get('name', '') for d in (raw.get('directors') or []) if isinstance(d, dict) and d.get('name'))
    date = (raw.get('date') or '')[:4]
    url = f"https://theporndb.net/movies/{raw.get('slug') or ext_id}"
    conn.execute('UPDATE movies SET ext_source = ?, ext_id = ?, ext_url = ?, ext_raw_json = ?, '
                 "year = COALESCE(NULLIF(year, ''), ?), studio = COALESCE(NULLIF(studio, ''), ?), "
                 "director = COALESCE(NULLIF(director, ''), ?), description = COALESCE(NULLIF(description, ''), ?), "
                 'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                 ('tpdb', str(ext_id), url, json.dumps(raw, ensure_ascii=False)[:500000], date or None,
                  site.get('name') or None, directors or None, (raw.get('description') or '').strip() or None, mid))


def link_from_video(conn, mid, video_id, commit=True):
    """A video linked to a ThePornDB movie (or a scene that names its movie): link the movie too,
    if it isn't linked yet. No network."""
    r = conn.execute('SELECT 1 FROM movies WHERE id = ? AND ext_id IS NULL', (mid,)).fetchone()
    if not r:
        return False
    row = conn.execute("SELECT ext_kind, ext_id, ext_raw_json FROM video_ext_link WHERE video_id = ? AND ext_source = 'tpdb'",
                       (video_id,)).fetchone()
    if not row:
        return False
    try:
        raw = json.loads(row[2] or '{}')
    except ValueError:
        return False
    if row[0] != 'movie':
        raw = next((m for m in (raw.get('movies') or []) if isinstance(m, dict) and m.get('id')), None)
        if not raw:
            return False
    ext_id = raw.get('id') or row[1]
    if conn.execute("SELECT 1 FROM movies WHERE ext_source = 'tpdb' AND ext_id = ? AND id != ?", (str(ext_id), mid)).fetchone():
        return False
    _fill_from_tpdb(conn, mid, raw, ext_id)
    if commit:
        conn.commit()
    return True


def on_video_linked(conn, video_id, kind, raw, scene_number=None, image=None):
    """ext_db: a video was just linked on ThePornDB. As a movie scene (or a scene that names its
    movie): put the video in that movie if it has none, and link the movie if it isn't."""
    ensure_schema(conn)
    if kind == 'movie':
        mraw, n = raw, scene_number
    else:
        mraw = next((m for m in (raw.get('movies') or []) if isinstance(m, dict) and m.get('title')), None)
        n, image = None, None
    if not mraw or not mraw.get('title'):
        return None
    ext_id = str(mraw.get('id') or '')
    row = conn.execute("SELECT id FROM movies WHERE ext_source = 'tpdb' AND ext_id = ?", (ext_id,)).fetchone() if ext_id else None
    have = conn.execute('SELECT movie_id FROM video_movie WHERE video_id = ?', (video_id,)).fetchone()
    mid = have[0] if have else (row[0] if row else get_or_create(conn, mraw['title']))
    if not have:
        if n is None:
            vt = conn.execute('SELECT title, filename_base FROM videos WHERE video_id = ?', (video_id,)).fetchone()
            own = parse_video(vt[0], vt[1]) if vt else None
            if own and title_key(own['movie']) == title_key(mraw['title']):
                n = own['scene']
        conn.execute('INSERT INTO video_movie (video_id, movie_id, scene, whole, source) VALUES (?, ?, ?, 0, ?)',
                     (video_id, mid, n, 'ext'))
    linked = conn.execute('SELECT ext_id, cover IS NOT NULL FROM movies WHERE id = ?', (mid,)).fetchone()
    if linked and not linked[0] and not (ext_id and row and row[0] != mid):
        _fill_from_tpdb(conn, mid, mraw, ext_id or mraw.get('slug'))
        if image and not linked[1]:
            set_cover(conn, mid, image[0], image[1] or 'image/jpeg', source='ext')
    conn.commit()
    return mid


def tpdb_candidates(query):
    import ext_db
    out = []
    for c in ext_db.tpdb_search('movie', query, per_page=12):
        out.append({'id': c['id'], 'title': c['title'], 'date': c['date'], 'studio': c['studio_name'] or '',
                    'image_url': c['image_url'], 'scenes': len(c['scenes']), 'page_url': c['page_url']})
    return out


def _tpdb_id(text):
    t = (text or '').strip()
    m = re.search(r'theporndb\.net/movies/([^/?#\s]+)', t)
    return m.group(1) if m else t


def link_tpdb(conn, mid, ext_id_or_url, keep_cover=None):
    """Link a movie to a ThePornDB movie (by id, slug or page link): its details fill the movie's
    empty ones and its front cover becomes the cover unless you set your own. Scenes aren't touched."""
    import ext_db
    get(conn, mid)
    ext_id = _tpdb_id(ext_id_or_url)
    if not ext_id:
        raise ValueError('Paste a ThePornDB movie link or ID')
    raw = ext_db.tpdb_find('movie', ext_id)
    real_id = str(raw.get('id') or ext_id)
    other = conn.execute("SELECT id, title FROM movies WHERE ext_source = 'tpdb' AND ext_id = ? AND id != ?", (real_id, mid)).fetchone()
    if other:
        raise ValueError(f'That ThePornDB movie is already linked to "{other[1]}"')
    _fill_from_tpdb(conn, mid, raw, real_id)
    r = conn.execute('SELECT cover_source FROM movies WHERE id = ?', (mid,)).fetchone()
    if (r[0] or '') != 'user':
        img_url = raw.get('image') or ext_db._tpdb_image(raw)
        img = ext_db.download_image(img_url) if img_url else None
        if img:
            set_cover(conn, mid, img[0], img[1] or 'image/jpeg', source='ext')
    conn.commit()
    return get(conn, mid)


def unlink_tpdb(conn, mid):
    ensure_schema(conn)
    conn.execute("UPDATE movies SET ext_source = NULL, ext_id = NULL, ext_url = NULL, ext_raw_json = NULL, "
                 "cover = CASE WHEN cover_source = 'ext' THEN NULL ELSE cover END, "
                 "cover_mime = CASE WHEN cover_source = 'ext' THEN NULL ELSE cover_mime END, "
                 "cover_source = CASE WHEN cover_source = 'ext' THEN NULL ELSE cover_source END, "
                 'updated_at = CURRENT_TIMESTAMP WHERE id = ?', (mid,))
    conn.commit()
    return get(conn, mid)
