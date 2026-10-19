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


_MOVIES_TABLE = '''CREATE TABLE IF NOT EXISTS {name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        title_key TEXT NOT NULL,
        edition TEXT NOT NULL DEFAULT '',                         -- tells same-named movies apart: 'Road Trip (2004)'
        edition_key TEXT NOT NULL DEFAULT '',
        favorite INTEGER NOT NULL DEFAULT 0,
        year TEXT, studio TEXT, director TEXT, description TEXT,
        cover BLOB, cover_mime TEXT, cover_source TEXT,          -- 'user' | 'ext'
        cover_video_id TEXT,
        ext_source TEXT, ext_id TEXT, ext_url TEXT, ext_raw_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'''
_OLD_COLUMNS = ('id, title, title_key, year, studio, director, description, cover, cover_mime, cover_source, cover_video_id, '
                'ext_source, ext_id, ext_url, ext_raw_json, created_at, updated_at')


def _migrate_names(conn):
    """Once: the first movies table had title_key UNIQUE (one movie per name). Rebuild it with an edition
    label and favorite, unique on (name, edition) instead. Every row and id is kept."""
    try:
        conn.execute('BEGIN')
        conn.execute(_MOVIES_TABLE.format(name='movies_new'))
        conn.execute(f'INSERT INTO movies_new ({_OLD_COLUMNS}) SELECT {_OLD_COLUMNS} FROM movies')
        n = conn.execute('SELECT COUNT(*) FROM movies_new').fetchone()[0]
        conn.execute('DROP TABLE movies')
        conn.execute('ALTER TABLE movies_new RENAME TO movies')
        conn.commit()
        print(f"[movies] movies table updated for same-named movies and favorites ({n} movies kept)")
    except Exception:
        conn.rollback()
        raise


def ensure_schema(conn):
    if id(conn) in _schema_ready:
        return
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN "
                                       "('movies', 'video_movie', 'idx_video_movie_movie', 'movie_tags', 'movie_ext_check', "
                                       "'ux_movies_name')")}
    if len(have) == 6:                       # already there: no commit (it could split a caller's transaction)
        _schema_ready.add(id(conn))
        return
    conn.commit()
    if 'movies' in have and 'edition' not in {r[1] for r in conn.execute('PRAGMA table_info(movies)')}:
        _migrate_names(conn)
    conn.execute(_MOVIES_TABLE.format(name='movies'))
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_movies_name ON movies(title_key, edition_key)')
    conn.execute('''CREATE TABLE IF NOT EXISTS video_movie (
        video_id TEXT PRIMARY KEY,
        movie_id INTEGER NOT NULL,
        scene INTEGER, part INTEGER,
        whole INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'manual')''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_video_movie_movie ON video_movie(movie_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS movie_tags (
        movie_id INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY (movie_id, value))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS movie_ext_check (
        movie_id INTEGER PRIMARY KEY,
        outcome TEXT NOT NULL,                     -- linked | review | not_found | error
        detail TEXT, candidates_json TEXT, run_id TEXT,
        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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


_SMALL = {'a', 'an', 'the', 'and', 'or', 'nor', 'but', 'of', 'in', 'on', 'at', 'to', 'for', 'by', 'with'}
_PRONOUNS = {'i', 'you', 'we', 'they', 'he', 'she', 'it', 'who', 'there', 'that', 'what', 'here', 'where'}


def _apostrophes(t):
    """Apostrophes that filenames lose: 'Neighbor s a tease' -> "Neighbor's a tease", 'It s' -> "It's",
    'Ain t' -> "Ain't", 'I m' -> "I'm", 'you re' -> "you're"."""
    out = []
    for tok in (t or '').split(' '):
        low, prev = tok.lower(), (out[-1].lower() if out else '')
        if out and ((low == 's' and re.fullmatch(r"[a-z]{2,}", prev))
                    or (low == 't' and re.fullmatch(r"[a-z]*n", prev))
                    or (low == 'm' and prev == 'i')
                    or (low in ('re', 've', 'll', 'd') and prev in _PRONOUNS)):
            out[-1] += "'" + tok.lower()
        else:
            out.append(tok)
    return ' '.join(out)


def _nice(t):
    """Apostrophes back, then: ALL CAPS, all lowercase or only a first capital -> Title Case, and in
    Title Case small words like 'a', 'of', 'the' are lowercase after the first. Other mixed case
    ('POV', 'McKenzie') is kept as typed."""
    t = _apostrophes(t)
    if not t:
        return t
    words = t.split(' ')
    if t == t.upper() or t[1:] == t[1:].lower():
        words = [w[:1].upper() + w[1:].lower() for w in words]
    elif not all(w[:1].isupper() or not w[:1].isalpha() for w in words):
        return t
    return ' '.join(w.lower() if i and w.lower() in _SMALL else w for i, w in enumerate(words))


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
    t = re.sub(r'[\s_]+', ' ', (title or '')).strip()
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
        return {'movie': _nice(_camel_words(m.group('m'))), 'scene': int(m.group('n')), 'part': None, 'sure': False, 'how': 'filename'}
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


def display_name(title, edition=''):
    """'Road Trip' + '2004' -> 'Road Trip (2004)'."""
    return f'{title} ({edition})' if edition else (title or '')


_EDITION = re.compile(r'^(.*\S)\s*\(([^()]+)\)$')


def resolve_name(conn, text):
    """A typed movie name -> (movie id or None, title, edition).
    'Road Trip (2004)' is the movie with that title and edition label, if there is one; otherwise the
    whole text is the name ('Breakers (Disc 1)'). A bare name is the one without a label (else the
    latest updated). A new label on an existing name ('Road Trip (2010)') makes a new same-named movie."""
    ensure_schema(conn)
    t = clean_title(text)
    m = _EDITION.match(t)
    base, ed = (clean_title(m.group(1)), m.group(2).strip()) if m else (t, '')
    if m:
        r = conn.execute('SELECT id FROM movies WHERE title_key = ? AND edition_key = ?', (title_key(base), title_key(ed))).fetchone()
        if r:
            return r[0], base, ed
    r = conn.execute("SELECT id, edition FROM movies WHERE title_key = ? ORDER BY edition_key = '' DESC, updated_at DESC, id DESC",
                     (title_key(t),)).fetchone()
    if r:
        return r[0], t, r[1]
    if m and conn.execute('SELECT 1 FROM movies WHERE title_key = ?', (title_key(base),)).fetchone():
        return None, base, ed
    return None, t, ''


def find_movie(conn, title):
    return resolve_name(conn, title)[0]


def get_or_create(conn, title, edition=None):
    """The movie with this name (see resolve_name), or a new one. edition: an explicit label."""
    if edition is not None:
        title, ed = clean_title(title), clean_title(edition)
        r = conn.execute('SELECT id FROM movies WHERE title_key = ? AND edition_key = ?', (title_key(title), title_key(ed))).fetchone()
        mid = r[0] if r else None
    else:
        mid, title, ed = resolve_name(conn, title)
    if not title_key(title):
        raise ValueError('A movie needs a name')
    if mid:
        return mid
    return conn.execute('INSERT INTO movies (title, title_key, edition, edition_key) VALUES (?, ?, ?, ?)',
                        (title, title_key(title), ed, title_key(ed))).lastrowid


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
    keep = "ext_id IS NULL AND COALESCE(cover_source, '') != 'user' AND id NOT IN (SELECT movie_id FROM movie_tags)"
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
    for t in ('movie_tags', 'movie_ext_check'):
        conn.execute(f'DELETE FROM {t} WHERE movie_id NOT IN (SELECT id FROM movies)')
    conn.commit()
    return n, m


def info_map(conn, video_ids):
    """{video_id: (movie_id, title, scene, part, whole)}. No schema check (it commits): the tables
    are made at start-up, and add_movie_info treats a missing table as no movies."""
    ids, out = list({v for v in video_ids if v}), {}
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in conn.execute(f'SELECT vm.video_id, m.id, m.title, vm.scene, vm.part, vm.whole, m.edition FROM video_movie vm '
                              f'JOIN movies m ON m.id = vm.movie_id WHERE vm.video_id IN ({",".join("?" * len(chunk))})', chunk):
            out[r[0]] = (r[1], display_name(r[2], r[6]), r[3], r[4], r[5])     # 'Road Trip (2004)' when labelled
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
    return [display_name(r[0], r[1]) for r in conn.execute(
        'SELECT title, edition FROM movies WHERE title LIKE ? ORDER BY (title LIKE ?) DESC, title, edition LIMIT ?', (f'%{q}%', f'{q}%', limit))]


# ---------------------------------------------------------------------------
# the movie itself
# ---------------------------------------------------------------------------
_FIELDS = ('title', 'edition', 'year', 'studio', 'director', 'description', 'cover_video_id')


def get(conn, mid):
    ensure_schema(conn)
    cur = conn.execute('SELECT id, title, edition, favorite, year, studio, director, description, cover IS NOT NULL, cover_source, '
                       'cover_video_id, ext_source, ext_id, ext_url, ext_raw_json, created_at, updated_at FROM movies WHERE id = ?', (mid,))
    r = cur.fetchone()
    if not r:
        raise ValueError('That movie no longer exists')
    d = dict(zip([c[0] for c in cur.description], r))
    d['has_cover'] = bool(d.pop('cover IS NOT NULL'))
    d['favorite'] = bool(d['favorite'])
    d['display'] = display_name(d['title'], d['edition'])
    raw = d.pop('ext_raw_json')
    d['ext_scenes'] = _ext_scenes(raw)
    try:
        d['back_image_url'] = (json.loads(raw).get('back_image') or None) if raw else None
    except (ValueError, AttributeError):
        d['back_image_url'] = None
    return d


def set_favorite(conn, ids, favorite=True):
    ensure_schema(conn)
    ids = [int(i) for i in ids or [] if i]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        conn.execute(f'UPDATE movies SET favorite = ? WHERE id IN ({",".join("?" * len(chunk))})', [1 if favorite else 0] + chunk)
    conn.commit()
    return len(ids)


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
    cur = get(conn, mid)
    sets, params = [], []
    if 'title' in changes or 'edition' in changes:             # the name: title + edition label
        t = clean_title(changes.get('title', cur['title']))
        ed = clean_title(changes.get('edition', cur['edition']) or '')
        if not title_key(t):
            raise ValueError('A movie needs a name')
        other = conn.execute('SELECT id, title, edition FROM movies WHERE title_key = ? AND edition_key = ? AND id != ?',
                             (title_key(t), title_key(ed), mid)).fetchone()
        if other:
            if not merge:
                return {'conflict': {'id': other[0], 'title': display_name(other[1], other[2])}}
            merge_into(conn, [mid], other[0])
            return {'merged_into': other[0]}
        sets += ['title = ?', 'title_key = ?', 'edition = ?', 'edition_key = ?']
        params += [t, title_key(t), ed, title_key(ed)]
    for k in _FIELDS:
        if k not in changes or k in ('title', 'edition'):
            continue
        v = changes[k]
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
    conn.execute(f'UPDATE OR IGNORE movie_tags SET movie_id = ? WHERE movie_id IN ({ph})', [target_id] + src)
    conn.execute(f'DELETE FROM movie_tags WHERE movie_id IN ({ph})', src)
    conn.execute(f'DELETE FROM movie_ext_check WHERE movie_id IN ({ph})', src)
    conn.execute(f'DELETE FROM movies WHERE id IN ({ph})', src)
    conn.execute('UPDATE movies SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (target_id,))
    conn.commit()
    return n


def delete(conn, mid):
    """Delete a movie; its videos stay, just without a movie."""
    ensure_schema(conn)
    for t in ('video_movie', 'movie_tags', 'movie_ext_check'):
        conn.execute(f'DELETE FROM {t} WHERE movie_id = ?', (mid,))
    conn.execute('DELETE FROM movies WHERE id = ?', (mid,))
    conn.commit()


def split_off(conn, video_ids, edition):
    """Move these videos to another movie with the same name, told apart by an edition label
    ('Road Trip' -> 'Road Trip (2004)'). Returns the new (or existing) movie id."""
    ensure_schema(conn)
    ed = clean_title(edition or '')
    if not title_key(ed):
        raise ValueError('Give the new movie a label (like its year or studio) to tell it apart')
    ids = [v for v in video_ids or [] if v]
    r = conn.execute(f'SELECT m.id, m.title, m.edition_key FROM video_movie vm JOIN movies m ON m.id = vm.movie_id '
                     f'WHERE vm.video_id IN ({",".join("?" * len(ids)) or "NULL"}) LIMIT 1', ids).fetchone()
    if not r:
        raise ValueError('Those videos are not in a movie')
    if r[2] == title_key(ed):
        raise ValueError('That is the label the movie already has')
    mid = get_or_create(conn, r[1], edition=ed)
    conn.execute(f'UPDATE video_movie SET movie_id = ? WHERE video_id IN ({",".join("?" * len(ids))})', [mid] + ids)
    drop_empty(conn, [r[0]])
    conn.commit()
    return mid


def set_cover_from_url(conn, mid, url):
    """Your own cover from an image link (downloaded once and kept)."""
    import ext_db
    get(conn, mid)
    url = (url or '').strip()
    if not re.match(r'^https?://', url, re.I):
        raise ValueError('Paste a link that starts with http:// or https://')
    img = ext_db.download_image(url)
    if not img:
        raise ValueError('Could not download an image from that link')
    mime = ext_db.image_mime(img[0], img[1])
    if not mime:
        raise ValueError('That link is not an image')
    set_cover(conn, mid, img[0], mime)
    return get(conn, mid)


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


def _scoped(model, conn, library):
    """{movie_id: card dict} for the movies with a video in this library / group (all for All Videos)."""
    import home
    import library_stats
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
    checks = {r[0]: r[1] for r in conn.execute('SELECT movie_id, outcome FROM movie_ext_check')}
    extra = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, edition, favorite FROM movies WHERE edition != '' OR favorite = 1")}
    for m in movies.values():
        m['status'] = 'linked' if m['linked'] else ('review' if checks.get(m['id']) == 'review' else
                                                    'not_found' if checks.get(m['id']) == 'not_found' else 'unchecked')
        ed, fav = extra.get(m['id'], ('', 0))
        m['edition'], m['favorite'], m['display'] = ed, bool(fav), display_name(m['title'], ed)
    return movies


def _tag_field_names():
    try:
        from config import LIST_FIELDS
        return [f.lower() for f in LIST_FIELDS if f.lower() not in ('site', 'cast')]
    except Exception:
        return ['tags']


def tag_sets(conn, movie_ids=None):
    """({movie_id: {tag}}, {movie_id: {site lowercase: spelling}}): every tag / site of any of the movie's
    videos (any library), plus its own movie tags and studio. Tags are lowercase, like the sidebar."""
    want = None if movie_ids is None else set(movie_ids)
    tags, sites = {}, {}
    for f in _tag_field_names() + ['site']:
        try:
            rows = conn.execute(f'SELECT vm.movie_id, t.value FROM video_movie vm JOIN video_{f}_junction j ON j.video_id = vm.video_id '
                                f'JOIN video_{f} t ON t.id = j.{f}_id').fetchall()
        except Exception:
            continue                                   # a field without a table yet
        for mid, val in rows:
            if not val or (want is not None and mid not in want):
                continue
            if f == 'site':
                sites.setdefault(mid, {}).setdefault(str(val).strip().lower(), str(val).strip())
            else:
                tags.setdefault(mid, set()).add(str(val).strip().lower())
    for mid, val in conn.execute('SELECT movie_id, value FROM movie_tags'):
        if want is None or mid in want:
            tags.setdefault(mid, set()).add(val.lower())
    for mid, studio in conn.execute("SELECT id, studio FROM movies WHERE COALESCE(studio, '') != ''"):
        if want is None or mid in want:
            sites.setdefault(mid, {}).setdefault(studio.strip().lower(), studio.strip())
    return tags, sites


def _csv(v):
    return {x.strip().lower() for x in (v or '').split(',') if x.strip()} if isinstance(v, str) else {str(x).lower() for x in (v or [])}


def _filtered(model, conn, library, q='', status='', tags=None, tags_exclude=None, sites=None, sites_exclude=None, favorites=False):
    """The scoped movies after the search, link status and sidebar tag / site filters -> (list, tags, sites)."""
    movies = list(_scoped(model, conn, library).values())
    ql = title_key(q or '')
    if ql:
        movies = [m for m in movies if ql in title_key(m['display']) or ql in title_key(m['studio'])]
    if favorites:
        movies = [m for m in movies if m['favorite']]
    st = (status or '').lower()
    if st == 'unlinked':
        movies = [m for m in movies if m['status'] != 'linked']
    elif st:
        movies = [m for m in movies if m['status'] == st]
    inc_t, exc_t, inc_s, exc_s = _csv(tags), _csv(tags_exclude), _csv(sites), _csv(sites_exclude)
    tsets, ssets = tag_sets(conn, [m['id'] for m in movies])
    if inc_t or exc_t or inc_s or exc_s:
        def ok(m):
            t, s = tsets.get(m['id'], set()), set(ssets.get(m['id'], {}))
            return inc_t <= t and not (exc_t & t) and inc_s <= s and not (exc_s & s)
        movies = [m for m in movies if ok(m)]
    return movies, tsets, ssets


_STATUS_ORDER = {'review': 0, 'unchecked': 1, 'not_found': 2, 'linked': 3}


def listing(model, conn, library=None, q='', sort='title', page=1, limit=120, status='', tags=None, tags_exclude=None,
            sites=None, sites_exclude=None, favorites=False):
    """Movies with at least one video in this library / group (every movie for All Videos), filtered and sorted."""
    import home
    ensure_schema(conn)
    out, _t, _s = _filtered(model, conn, library, q, status, tags, tags_exclude, sites, sites_exclude, favorites)
    s = (sort or 'title').lower()
    out.sort(key=lambda m: _fold(m['display']).lower())
    if s == 'year':
        out.sort(key=lambda m: m['year'] or '0000', reverse=True)
    elif s == 'count':
        out.sort(key=lambda m: m['count'], reverse=True)
    elif s == 'added':
        out.sort(key=lambda m: m['added'], reverse=True)
    elif s == 'status':
        out.sort(key=lambda m: _STATUS_ORDER.get(m['status'], 9))
    elif s == 'favorites':
        out.sort(key=lambda m: not m['favorite'])
    total = len(out)
    start = (max(1, page) - 1) * limit
    out = out[start:start + limit]
    every = {}                                         # videos in all libraries ("2 here · 5 in all")
    ids = [m['id'] for m in out]
    if ids:
        w_all, p_all = home.scope_sql(model, conn, None)
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            every.update(conn.execute(f'SELECT vm.movie_id, COUNT(DISTINCT vm.video_id) FROM video_movie vm '
                                      f'JOIN videos v ON v.video_id = vm.video_id JOIN file_versions fv ON fv.video_id = v.video_id '
                                      f'WHERE vm.movie_id IN ({",".join("?" * len(chunk))}) AND {w_all} GROUP BY vm.movie_id',
                                      chunk + p_all).fetchall())
    for m in out:
        m['count_all'] = every.get(m['id'], m['count'])
        pick = m.pop('_cover') or (m['_first'][1:] if m['_first'] else None)
        m.pop('_first')
        m.pop('cover_video_id', None)
        m['cover'] = (f"/api/movies/{m['id']}/cover?v={m['updated_at'] or ''}" if m['has_cover']
                      else (_thumb(pick[1], pick[0], pick[2]) if pick else None))
    return {'movies': out, 'total': total}


def filter_counts(model, conn, library=None, q='', status='', tags=None, tags_exclude=None, sites=None, sites_exclude=None,
                  favorites=False):
    """The sidebar's tag / site lists for the Movies tab: how many of the shown movies have each one."""
    ensure_schema(conn)
    movies, tsets, ssets = _filtered(model, conn, library, q, status, tags, tags_exclude, sites, sites_exclude, favorites)
    tc, sc, spell = Counter(), Counter(), {}
    for m in movies:
        tc.update(tsets.get(m['id'], set()))
        for k, v in ssets.get(m['id'], {}).items():
            sc[k] += 1
            spell.setdefault(k, v)
    return {'tags': dict(tc), 'sites': {spell[k]: n for k, n in sc.items()}}


# ---------------------------------------------------------------------------
# movie tags (your own, for the Movies tab) and ThePornDB suggestions
# ---------------------------------------------------------------------------
def tag_format(value):
    """Your tag format: lowercase, spaces -> periods ('Big Tits' -> 'big.tits')."""
    s = re.sub(r'\s+', '.', (value or '').strip().lower())
    return re.sub(r'\.{2,}', '.', s).strip('.')


def movie_tags(conn, mid):
    ensure_schema(conn)
    return [r[0] for r in conn.execute('SELECT value FROM movie_tags WHERE movie_id = ? ORDER BY value', (mid,))]


def set_tags(conn, mid, add=(), remove=(), commit=True):
    ensure_schema(conn)
    get(conn, mid)
    for v in add or ():
        v = tag_format(v)
        if v:
            conn.execute('INSERT OR IGNORE INTO movie_tags (movie_id, value) VALUES (?, ?)', (mid, v))
    for v in remove or ():
        conn.execute('DELETE FROM movie_tags WHERE movie_id = ? AND value = ?', (mid, tag_format(v)))
    if commit:
        conn.commit()
    return movie_tags(conn, mid)


def _raw(conn, mid):
    r = conn.execute('SELECT ext_raw_json FROM movies WHERE id = ?', (mid,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except ValueError:
        return {}


def _tpdb_tag_values(conn, raw, vocab=None, mapped_only=False):
    """ThePornDB tags -> [{'ext': name, 'value': your tag, 'mapped': bool}] through your tag mappings
    (ext_db._tag_mapping). Ignored tags are left out; mapped_only: only auto / confirmed mappings."""
    import ext_db
    if vocab is None:
        vocab = ext_db._video_vocab(conn, ext_db._tag_fields())
    out, seen = [], set()
    for t in raw.get('tags') or []:
        name = (t.get('name') if isinstance(t, dict) else t) or ''
        if not name.strip():
            continue
        m = ext_db._tag_mapping(conn, 'tpdb', name, vocab)
        if m['status'] == 'ignored':
            continue
        mapped = m['status'] in ('auto', 'confirmed') and m.get('value')
        if mapped_only and not mapped:
            continue
        for v in (ext_db.split_values(m['value']) if mapped else [ext_db.user_tag_format(name)]):
            v = tag_format(v)
            if v and v not in seen:
                seen.add(v)
                out.append({'ext': name, 'value': v, 'mapped': bool(mapped)})
    return out


def suggestions(conn, mid):
    """What the linked ThePornDB movie suggests: tags the movie doesn't have, and details that differ."""
    d = get(conn, mid)
    raw = _raw(conn, mid)
    if not raw:
        return {'tags': [], 'details': []}
    have = set(movie_tags(conn, mid))
    tags = [t for t in _tpdb_tag_values(conn, raw) if t['value'] not in have]
    site = raw.get('site') or {}
    director = ', '.join(x.get('name', '') for x in (raw.get('directors') or []) if isinstance(x, dict) and x.get('name'))
    details = []
    for field, val in (('year', (raw.get('date') or '')[:4]), ('studio', site.get('name') or ''),
                       ('director', director), ('description', (raw.get('description') or '').strip())):
        if val and val != (d.get(field) or ''):
            details.append({'field': field, 'value': val, 'current': d.get(field) or ''})
    return {'tags': tags, 'details': details}


# ---------------------------------------------------------------------------
# bulk ThePornDB linking
# ---------------------------------------------------------------------------
AUTO_LEAD = 15
REVIEW_MIN = 40


def _movie_context(conn, mid):
    import ext_db
    m = conn.execute('SELECT title, year, studio FROM movies WHERE id = ?', (mid,)).fetchone()
    sites = Counter(r[0] for r in conn.execute(
        'SELECT t.value FROM video_movie vm JOIN video_site_junction j ON j.video_id = vm.video_id '
        'JOIN video_site t ON t.id = j.site_id WHERE vm.movie_id = ?', (mid,)))
    cast = set()
    for (name,) in conn.execute('SELECT DISTINCT t.value FROM video_movie vm JOIN video_cast_junction j ON j.video_id = vm.video_id '
                                'JOIN video_cast t ON t.id = j.cast_id WHERE vm.movie_id = ?', (mid,)):
        cast.add(ext_db.norm_name(name))
        try:
            cast.add(ext_db.norm_name(ext_db.resolve_primary(conn, name)))
        except Exception:
            pass
    top_scene = conn.execute('SELECT MAX(scene) FROM video_movie WHERE movie_id = ?', (mid,)).fetchone()[0] or 0
    studios = {ext_db.norm_name(x) for x in ([m[2]] if m[2] else []) + [s for s, _n in sites.most_common(2)] if x}
    return {'title': m[0], 'key': title_key(m[0]), 'year': (m[1] or '')[:4], 'studios': studios, 'cast': cast, 'top_scene': top_scene}


def score_candidate(ctx, cand):
    """-> (score, [reasons]) for one ThePornDB movie search result against your movie."""
    import difflib
    import ext_db
    pts, why = 0, []
    if title_key(cand.get('title')) == ctx['key']:
        pts += 60
        why.append('same title')
    else:
        sim = difflib.SequenceMatcher(None, ctx['key'], title_key(cand.get('title'))).ratio()
        pts += round(sim * 50)
        why.append(f'title {round(sim * 100)}% alike')
    if ctx['year'] and (cand.get('date') or '')[:4] == ctx['year']:
        pts += 15
        why.append('same year')
    names = {ext_db.norm_name(x) for x in (cand.get('studio_name'), cand.get('parent_name')) if x}
    if ctx['studios'] and any(a and b and (a in b or b in a) for a in ctx['studios'] for b in names):
        pts += 15
        why.append('same studio')
    perfs = set()
    for p in cand.get('performers') or []:
        perfs |= {ext_db.norm_name(p.get('name')), ext_db.norm_name(p.get('as'))}
    for sc in cand.get('scenes') or []:
        for p in sc.get('performers') or []:
            perfs |= {ext_db.norm_name(p.get('name')), ext_db.norm_name(p.get('as'))}
    shared = len((ctx['cast'] & perfs) - {''})
    if shared:
        pts += min(30, 10 * shared)
        why.append(f'{shared} performer{"" if shared == 1 else "s"} in it')
    if ctx['top_scene'] and len(cand.get('scenes') or []) >= ctx['top_scene']:
        pts += 5
        why.append(f"{len(cand['scenes'])} scenes")
    return pts, why


def bulk_one(conn, mid, add_tags=True):
    """Check one movie on ThePornDB -> (outcome, detail, candidates). Links it when the match is sure."""
    import ext_db
    ctx = _movie_context(conn, mid)
    found = ext_db.tpdb_search('movie', ctx['title'], per_page=10)
    scored = []
    for c in found:
        pts, why = score_candidate(ctx, c)
        scored.append({'id': c['id'], 'title': c['title'], 'date': c['date'], 'studio': c.get('studio_name') or '',
                       'image_url': c.get('image_url'), 'scenes': len(c.get('scenes') or []), 'page_url': c.get('page_url'),
                       'score': pts, 'reasons': why})
    scored.sort(key=lambda x: -x['score'])
    top5 = scored[:5]
    if not scored or scored[0]['score'] < REVIEW_MIN:
        return 'not_found', (f"Best: {scored[0]['title']} ({scored[0]['score']})" if scored else 'No results'), top5
    top = scored[0]
    same = [x for x in scored if 'same title' in x['reasons']]
    lead = top['score'] - (scored[1]['score'] if len(scored) > 1 else 0)
    agrees = any(r in ('same year', 'same studio') or r.endswith(' in it') for r in top['reasons'])
    if 'same title' in top['reasons'] and (len(same) == 1 or lead >= AUTO_LEAD) and agrees:
        link_tpdb(conn, mid, top['id'])
        detail = f"{top['title']} ({top['score']}: {', '.join(top['reasons'])})"
        if add_tags:
            vals = [t['value'] for t in _tpdb_tag_values(conn, _raw(conn, mid), mapped_only=True)]
            if vals:
                set_tags(conn, mid, add=vals)
                detail += f' · {len(vals)} tag{"" if len(vals) == 1 else "s"} added'
        return 'linked', detail, top5
    why = []
    if 'same title' not in top['reasons']:
        why.append('title differs')
    elif not (len(same) == 1 or lead >= AUTO_LEAD):
        why.append(f'{len(same)} movies with this title')
    if not agrees:
        why.append('nothing else agrees')
    return 'review', f"{top['title']} ({top['score']}) - {'; '.join(why)}", top5


def _log_check(conn, mid, outcome, detail, cands, run_id=None):
    conn.execute('INSERT OR REPLACE INTO movie_ext_check (movie_id, outcome, detail, candidates_json, run_id, checked_at) '
                 'VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                 (mid, outcome, detail, json.dumps(cands or [], ensure_ascii=False), run_id))
    conn.commit()


def dismiss(conn, mid):
    """'None of these': not on ThePornDB."""
    ensure_schema(conn)
    conn.execute("UPDATE movie_ext_check SET outcome = 'not_found', detail = 'None of the matches (your choice)' WHERE movie_id = ?", (mid,))
    if not conn.execute('SELECT changes()').fetchone()[0]:
        _log_check(conn, mid, 'not_found', 'None of the matches (your choice)', [])
    conn.commit()


def check_of(conn, mid):
    ensure_schema(conn)
    r = conn.execute('SELECT outcome, detail, candidates_json, checked_at FROM movie_ext_check WHERE movie_id = ?', (mid,)).fetchone()
    if not r:
        return None
    try:
        cands = json.loads(r[2] or '[]')
    except ValueError:
        cands = []
    return {'outcome': r[0], 'detail': r[1], 'candidates': cands, 'checked_at': r[3]}


BULK_OUTCOMES = {'linked': 'Linked', 'review': 'Needs review', 'not_found': 'Not found', 'error': 'Error'}
_bulk = {'running': False, 'stop': False, 'run_id': None, 'total': 0, 'done': 0, 'counts': {}, 'recent': [],
         'current': None, 'error': None, 'note': '', 'started_at': None, 'finished_at': None}
_bulk_lock = __import__('threading').Lock()


def bulk_targets(model, conn, library=None, skip_checked=True, movie_ids=None):
    """Unlinked movies in this library (or these ids) to check, in name order."""
    ensure_schema(conn)
    if movie_ids:
        ids = [m for (m,) in conn.execute(f'SELECT id FROM movies WHERE ext_id IS NULL AND id IN ({",".join("?" * len(movie_ids))})',
                                         list(movie_ids))]
        rows = {m: conn.execute('SELECT title FROM movies WHERE id = ?', (m,)).fetchone()[0] for m in ids}
    else:
        rows = {m['id']: m['title'] for m in _scoped(model, conn, library).values() if not m['linked']}
    if skip_checked:
        done = {r[0] for r in conn.execute("SELECT movie_id FROM movie_ext_check WHERE outcome != 'error'")}
        rows = {k: v for k, v in rows.items() if k not in done}
    return [k for k, _v in sorted(rows.items(), key=lambda kv: _fold(kv[1]).lower())]


def bulk_status():
    import time
    st = {k: v for k, v in _bulk.items() if k != 'stop'}
    st['recent'] = list(_bulk['recent'])
    st['counts'] = dict(_bulk['counts'])
    if st.get('started_at'):
        end = st.get('finished_at') or time.time()
        st['elapsed_seconds'] = round(end - st['started_at'])
        st['eta_seconds'] = (round((end - st['started_at']) / st['done'] * (st['total'] - st['done']))
                             if st['running'] and st['done'] else None)
    st['outcome_labels'] = BULK_OUTCOMES
    return st


def start_bulk(model, targets, add_tags=True, delay=1.0):
    import threading
    import time
    import uuid
    with _bulk_lock:
        if _bulk['running']:
            raise ValueError('A bulk link is already running')
        _bulk.update(running=True, stop=False, run_id=uuid.uuid4().hex[:12], total=len(targets), done=0, counts={}, recent=[],
                     current=None, error=None, note='', started_at=time.time(), finished_at=None)
    threading.Thread(target=_bulk_worker, args=(model, list(targets), add_tags, delay), daemon=True, name='movies-bulk').start()
    return bulk_status()


def stop_bulk():
    _bulk['stop'] = True


def _bulk_worker(model, targets, add_tags, delay):
    import time
    import ext_db
    st = _bulk
    ext_db._pace_interval = max(0.0, float(delay or 0))
    errors = 0
    try:
        conn = model.db.get_connection()
        ensure_schema(conn)
        for idx, mid in enumerate(targets):
            if st['stop']:
                break
            r = conn.execute('SELECT title, ext_id FROM movies WHERE id = ?', (mid,)).fetchone()
            if not r or r[1]:
                st['done'] = idx + 1
                continue
            st['current'] = r[0]
            outcome, detail, cands = 'error', 'Rate limited', []
            for attempt in range(3):
                try:
                    outcome, detail, cands = bulk_one(conn, mid, add_tags)
                    break
                except ext_db.ExtDBError as e:
                    msg = str(e)
                    if ('rate limit' in msg.lower() or '429' in msg) and attempt < 2:
                        st['note'] = 'Rate limited by ThePornDB - pausing for 60 seconds'
                        end = time.monotonic() + 60
                        while time.monotonic() < end and not st['stop']:
                            time.sleep(0.5)
                        st['note'] = ''
                        continue
                    if 'api key' in msg.lower():
                        raise
                    outcome, detail = 'error', msg
                    break
                except Exception as e:
                    outcome, detail = 'error', f'{type(e).__name__}: {e}'
                    break
            try:
                _log_check(conn, mid, outcome, detail, cands, st['run_id'])
            except Exception as e:
                print(f"[movies] bulk: could not log {mid}: {e}")
            st['counts'][outcome] = st['counts'].get(outcome, 0) + 1
            st['recent'].insert(0, {'id': mid, 'title': r[0], 'outcome': outcome, 'outcome_label': BULK_OUTCOMES.get(outcome, outcome),
                                    'detail': detail})
            del st['recent'][40:]
            st['done'] = idx + 1
            errors = errors + 1 if outcome == 'error' else 0
            if errors >= 10:
                st['error'] = f'Stopped after 10 errors in a row. Last error: {detail}'
                break
    except ext_db.ExtDBError as e:
        st['error'] = str(e)
    except Exception as e:
        st['error'] = f'Unexpected error: {e}'
    finally:
        ext_db._pace_interval = 0.0
        st.update(running=False, current=None, note='', finished_at=time.time())
        print(f"[movies] bulk link {st['run_id']} finished: {st['done']}/{st['total']} {st['counts']}")


def detail(model, conn, mid, username=None, watch_history=None):
    """A movie, its videos in order (the same records as the Library) and its ThePornDB scene list."""
    import home
    import library_stats
    d = get(conn, mid)
    where, params = home.scope_sql(model, conn, None)
    rows = conn.execute(f'SELECT fv.path, vm.scene, vm.part, vm.whole, v.title, fv.length_seconds, v.video_id, v.library FROM video_movie vm '
                        f'JOIN videos v ON v.video_id = vm.video_id JOIN file_versions fv ON fv.video_id = v.video_id '
                        f'WHERE vm.movie_id = ? AND {where}', [mid] + params).fetchall()
    rows = sorted(rows, key=lambda r: _order(r[1], r[2], r[3], r[4]))
    items = [x for x in (home.card(model, r[0], username, watch_history, extra={'library': r[7]}) for r in rows) if x]
    library_stats.add_play_stats(conn, username, items)
    d['libraries'] = sorted({r[7] for r in rows if r[7]})
    for i in items:                                    # the '2×' versions badge, as in the Library grid
        try:
            vs = model.db.get_file_versions(i['video_id'])
            if vs and len(vs) > 1:
                i['versions'] = vs
        except Exception:
            pass
    d['tags'] = movie_tags(conn, mid)
    d['check'] = None if d['ext_id'] else check_of(conn, mid)
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
            name = _nice(clean_title(title) or filename_text(fbase))
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
    existing = {}
    for r in conn.execute("SELECT title_key, id, title, edition FROM movies ORDER BY edition_key = '' DESC, updated_at DESC"):
        existing.setdefault(r[0], (r[1], display_name(r[2], r[3])))   # a bare name goes to the one without a label
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
