# This file is: ./images.py
"""
Images in your library folders: an Images tab per library, albums by folder, a viewer.

The library scan (model.scan_library) hands over every image file it walked past; sync() keeps the
`images` table in step (new / gone / back). Folders named like screenshot folders (skip_folders,
Settings -> Browsing) are left out when listing, so changing that list shows at once.
Thumbnails are made on first view (Pillow) and kept in CACHE_DIR/images.

A .zip counts as a folder: its images are rows like any other, path <zip>/<name inside>, so albums,
skipped folders and favorites just work. Nothing is unpacked: `archive` + `member` say where to read
one from. What's in a zip is kept in image_archives and only read again when the zip changes.

An album (a folder) has what a scene has, album-wide: a title of your own, cast, site, tags and the other
list fields (image_album_meta, one row per value), and the videos it goes with (image_album_videos, by
video_id). Photos themselves have no tags. Width / height are read from the file's header when an album
is first opened (fill_sizes), so its grid can show every photo whole.

No FastAPI imports.
"""
import difflib
import io
import json
import os
import re
import threading
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
SKIP_KEY = 'image_skip_folders'
DEFAULT_SKIP = ['screens', 'screen', 'thumbnails', 'thumbs', 'screenshots']
THUMB_SIZE = 480
TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
         '.gif': 'image/gif', '.bmp': 'image/bmp', '.avif': 'image/avif'}

ARCHIVE_EXTENSIONS = {'.zip'}
ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}     # what Python can read

_thumb_slots = threading.Semaphore(6)         # a big album opening doesn't make 50 at once (reading the file is half the time)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        library TEXT,
        root TEXT,
        folder TEXT,
        filename TEXT,
        size INTEGER,
        mtime INTEGER,
        width INTEGER,
        height INTEGER,
        favorite INTEGER DEFAULT 0,
        added_at TEXT,
        deleted_at TEXT)''')
    have = {r[1] for r in conn.execute('PRAGMA table_info(images)')}
    for col in ('archive', 'member'):                                       # in a zip: which one, and its name there
        if col not in have:
            conn.execute(f'ALTER TABLE images ADD COLUMN {col} TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_images_lib_folder ON images(library, folder)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_images_deleted ON images(deleted_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS image_albums (
        folder TEXT PRIMARY KEY,
        favorite INTEGER DEFAULT 0,
        cover_id INTEGER)''')
    if 'title' not in {r[1] for r in conn.execute('PRAGMA table_info(image_albums)')}:
        conn.execute('ALTER TABLE image_albums ADD COLUMN title TEXT')         # your own name; NULL = the folder's
    conn.execute('''CREATE TABLE IF NOT EXISTS image_archives (
        path TEXT PRIMARY KEY,
        size INTEGER,
        mtime INTEGER,
        members TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS image_album_meta (
        folder TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY (folder, field, value))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS image_album_videos (
        folder TEXT NOT NULL, video_id TEXT NOT NULL,
        PRIMARY KEY (folder, video_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_image_album_videos_video ON image_album_videos(video_id)')
    conn.commit()


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def is_archive(name):
    return os.path.splitext(name)[1].lower() in ARCHIVE_EXTENSIONS


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def sync(conn, library, roots, paths):
    """After a scan: `paths` = the images now found under `roots` (the library's roots that were
    walked; a .zip stands for the images in it). New ones are added, ones no longer there are marked gone, ones back are restored.
    Roots that weren't walked (an offline share) are left alone. -> {added, removed, restored}"""
    ensure_schema(conn)
    roots = [os.path.normpath(r) for r in roots]
    found = {os.path.normpath(p) for p in paths}
    zips = {p for p in found if is_archive(p)}
    in_zips, reread = _zip_contents(conn, zips)                             # before any write: slow on a network share
    found = (found - zips) | set(in_zips)
    now = datetime.now().isoformat()
    under = ' OR '.join(['path LIKE ? ESCAPE \'\\\''] * len(roots)) or '0'
    likes = [_like_prefix(r) for r in roots]
    known = {r[0]: r[1] for r in conn.execute(f'SELECT path, deleted_at FROM images WHERE {under}', likes)}
    added = restored = removed = 0
    for p in found:
        if p not in known:
            archive, member, size, mtime = in_zips.get(p, (None, None, None, None))
            if not archive:
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                size, mtime = st.st_size, int(st.st_mtime)
            root = max((r for r in roots if p.startswith(r + os.sep)), key=len, default=None)
            conn.execute('INSERT OR IGNORE INTO images (path, library, root, folder, filename, size, mtime, added_at, archive, member) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (p, library, root, os.path.dirname(p), os.path.basename(p), size, mtime, now, archive, member))
            added += 1
        elif known[p] is not None:
            conn.execute('UPDATE images SET deleted_at = NULL, library = ? WHERE path = ?', (library, p))
            restored += 1
    changed = {r[0] for r in reread}
    for z, size, mtime, members in reread:
        conn.execute('INSERT OR REPLACE INTO image_archives (path, size, mtime, members) VALUES (?, ?, ?, ?)',
                     (z, size, mtime, json.dumps(members)))
    for p, (z, _member, size, mtime) in in_zips.items():
        if p in known and z in changed:                      # replaced: the thumbnail goes with its time
            conn.execute('UPDATE images SET size = ?, mtime = ? WHERE path = ?', (size, mtime, p))
    gone = [p for p, d in known.items() if d is None and p not in found]
    for k in range(0, len(gone), 500):
        chunk = gone[k:k + 500]
        removed += conn.execute(f'UPDATE images SET deleted_at = ? WHERE path IN ({",".join("?" * len(chunk))})',
                                [now] + chunk).rowcount
    conn.commit()
    return {'added': added, 'removed': removed, 'restored': restored}


def _read_zip(path):
    """[[name inside, size]] of the images in a zip we can read. Not a zip, or still downloading -> [].
    A share that dropped raises OSError."""
    try:
        with zipfile.ZipFile(path) as z:
            return [[i.filename, i.file_size] for i in z.infolist()
                    if not i.is_dir() and is_image(i.filename) and not i.flag_bits & 1      # 1: password protected
                    and i.compress_type in ZIP_METHODS and _inside(i.filename)]
    except (zipfile.BadZipFile, ValueError, NotImplementedError, EOFError) as e:
        print(f'[images] skipping {path}: {e}')
        return []


def _inside(member):
    """No Mac leftovers, nothing that points outside the zip ('../x.jpg')."""
    parts = member.replace('\\', '/').split('/')
    return not (parts[0] in ('', '__MACOSX') or '..' in parts or parts[-1].startswith('._'))


def _zip_contents(conn, zips):
    """-> {path: (zip, name inside, size, zip's time)}, [(zip, size, mtime, members) read again]."""
    cached = {r[0]: r[1:] for r in conn.execute('SELECT path, size, mtime, members FROM image_archives')}
    out, reread = {}, []
    for z in zips:
        old = cached.get(z)
        try:
            st = os.stat(z)
            size, mtime = st.st_size, int(st.st_mtime)
            if old and old[:2] == (size, mtime):
                members = json.loads(old[2])
            else:
                members = _read_zip(z)
                reread.append((z, size, mtime, members))
        except OSError:                                                     # unreachable: keep what we knew
            if not old:
                continue
            mtime, members = old[1], json.loads(old[2])
        for name, bytes_ in members:
            p = os.path.normpath(os.path.join(z, name.replace('\\', '/')))
            out[p] = (z, name, bytes_, mtime)
    return out, reread


def _like_prefix(root):
    return re.sub(r'([\\%_])', r'\\\1', root.rstrip(os.sep) + os.sep) + '%'


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def skip_folders(conn):
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SKIP_KEY,)).fetchone()
        if row:
            return [s for s in json.loads(row[0]) if s]
    except Exception:
        pass
    return list(DEFAULT_SKIP)


def set_skip_folders(conn, names):
    clean = []
    for n in names or []:
        n = str(n).strip().lower()
        if n and n not in clean:
            clean.append(n)
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SKIP_KEY, json.dumps(clean)))
    conn.commit()
    return clean


def skipped(folder, root, skip):
    """Is this folder (or a folder above it, below the library root) named one of the skipped names,
    or has one as a word ('Straight screens')?"""
    if not skip:
        return False
    rel = os.path.relpath(folder, root) if root and (folder == root or folder.startswith(root + os.sep)) else folder
    for part in rel.split(os.sep):
        p = part.lower()
        if p not in ('', '.') and (p in skip or any(w in skip for w in re.findall(r'[a-z0-9]+', p))):
            return True
    return False


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s or '')]


def _folders(conn, libraries):
    """{folder: {root, library, count, newest, first_id}} for these libraries, skipped folders left out."""
    if not libraries:
        return {}
    skip = set(skip_folders(conn))
    rows = conn.execute(f'''SELECT folder, root, library, COUNT(*), MAX(mtime), SUM(favorite) FROM images
                            WHERE deleted_at IS NULL AND library IN ({",".join("?" * len(libraries))})
                            GROUP BY folder''', list(libraries)).fetchall()
    return {r[0]: {'root': r[1], 'library': r[2], 'count': r[3], 'newest': r[4] or 0, 'favorites': r[5] or 0}
            for r in rows if not skipped(r[0], r[1], skip)}


def title(folder):
    """An album's name: its folder's, a zip's without the .zip."""
    base = os.path.basename(folder)
    return os.path.splitext(base)[0] if is_archive(base) else base


def counts(conn, libraries):
    """{library: images shown} (skipped folders left out)."""
    out = {}
    for f in _folders(conn, libraries).values():
        out[f['library']] = out.get(f['library'], 0) + f['count']
    return out


CAST_FIELDS = ('cast', 'male_cast')
TEXT_FIELDS = ('year', 'rating')                  # one value each; the title lives in image_albums


def _tag_fields():
    try:
        from config import LIST_FIELDS
        return {f.lower() for f in LIST_FIELDS} - {'site', 'cast'}
    except Exception:
        return {'tags'}


def _csv(v):
    if isinstance(v, str):
        return {x.strip().lower() for x in v.split(',') if x.strip()}
    return {str(x).strip().lower() for x in (v or []) if str(x).strip()}


def albums(conn, libraries, sort='name', q='', tags=None, tags_exclude=None, sites=None, sites_exclude=None,
           cast=None, favorites=False, covers=True):
    """The albums of these libraries, searched (folder, name and every value), filtered like the sidebar
    filters movies (tags = every list field but site / cast, lowercase; sites; cast) and sorted."""
    folders = _folders(conn, libraries)
    if not folders:
        return []
    extra = {r[0]: (r[1], r[2], r[3]) for r in conn.execute('SELECT folder, favorite, cover_id, title FROM image_albums')}
    meta = album_meta(conn)
    base_count = {}
    for f in folders:
        b = os.path.basename(f).lower()
        base_count[b] = base_count.get(b, 0) + 1
    out = []
    ql = (q or '').strip().lower()
    inc_t, exc_t, inc_s, exc_s, inc_c = _csv(tags), _csv(tags_exclude), _csv(sites), _csv(sites_exclude), _csv(cast)
    tag_fields = _tag_fields()
    for f, d in folders.items():
        fav, cover, own = extra.get(f, (0, None, None))
        m = meta.get(f, {})
        base = os.path.basename(f)
        name = own or title(f)
        if not own and base_count[base.lower()] > 1 and f != d['root']:     # several 'Images' folders: 'Set / Images'
            name = f'{os.path.basename(os.path.dirname(f))} / {base}'
        if ql and ql not in f.lower() and ql not in name.lower() and not any(ql in v.lower() for vs in m.values() for v in vs):
            continue
        if favorites and not fav:
            continue
        people = sorted({v for k in CAST_FIELDS for v in m.get(k, [])}, key=str.lower)
        if inc_c and not inc_c <= {p.lower() for p in people}:
            continue
        if inc_t or exc_t or inc_s or exc_s:
            t = {v.lower() for k, vs in m.items() if k in tag_fields for v in vs}
            s = {v.lower() for v in m.get('site', [])}
            if not (inc_t <= t and not (exc_t & t) and inc_s <= s and not (exc_s & s)):
                continue
        out.append({'folder': f, 'name': name, 'library': d['library'], 'count': d['count'], 'newest': d['newest'],
                    'favorite': bool(fav), 'cover_id': cover, 'cast': sorted(m.get('cast', []), key=str.lower) or people,
                    'renamed': bool(own)})
    keys = {'newest': lambda a: (-a['newest'], _natural(a['name'])),
            'count': lambda a: (-a['count'], _natural(a['name'])),
            'favorites': lambda a: (not a['favorite'], _natural(a['name'])),
            'cast': lambda a: (not a['cast'], (a['cast'] or [''])[0].lower(), _natural(a['name']))}
    out.sort(key=keys.get(sort, lambda a: _natural(a['name'])))
    if covers:
        add_covers(conn, out)
    return out


def listing(conn, libraries, sort='name', q='', page=1, limit=120, **filters):
    """One page of albums() -> {albums, total, letters}. letters: where each # A-Z starts (by name, or by
    first cast name when sorted by cast; albums without cast come last and get none)."""
    out = albums(conn, libraries, sort, q, covers=False, **filters)
    letters = _letter_index(out, sort) if sort in ('name', 'cast') else []
    limit = max(1, int(limit or 120))
    start = (max(1, int(page or 1)) - 1) * limit
    shown = out[start:start + limit]
    add_covers(conn, shown)
    return {'albums': shown, 'total': len(out), 'letters': letters}


def _letter_index(albums_, sort):
    out, seen = [], set()
    for i, a in enumerate(albums_):
        text = (a['cast'] or [''])[0] if sort == 'cast' else a['name']
        if not text:
            continue
        ch = text.strip()[:1].upper()
        L = ch if 'A' <= ch <= 'Z' else '#'
        if L not in seen:
            seen.add(L)
            out.append({'letter': L, 'index': i})
    return out


def filter_counts(conn, libraries, q='', **filters):
    """The sidebar's Tags / Sites for the Images tab: how many of the shown albums have each one."""
    shown = albums(conn, libraries, 'name', q, covers=False, **filters)
    meta, tag_fields = album_meta(conn, [a['folder'] for a in shown]), _tag_fields()
    tc, sc, spell = Counter(), Counter(), {}
    for a in shown:
        m = meta.get(a['folder'], {})
        tc.update({v.lower() for k, vs in m.items() if k in tag_fields for v in vs})
        for v in {v.strip() for v in m.get('site', [])}:
            sc[v.lower()] += 1
            spell.setdefault(v.lower(), v)
    return {'tags': dict(tc), 'sites': {spell[k]: n for k, n in sc.items()}}


def add_covers(conn, out):
    """Each album's cover: the chosen one if it's still there, else the first image by name."""
    need = [a for a in out if not a['cover_id']]
    chosen = [a['cover_id'] for a in out if a['cover_id']]
    alive = set()
    for k in range(0, len(chosen), 500):
        chunk = chosen[k:k + 500]
        alive |= {r[0] for r in conn.execute(f'SELECT id FROM images WHERE deleted_at IS NULL AND id IN ({",".join("?" * len(chunk))})', chunk)}
    for a in out:
        if a['cover_id'] and a['cover_id'] not in alive:
            a['cover_id'] = None
            need.append(a)
    firsts = {}
    folders_needed = [a['folder'] for a in need]
    for k in range(0, len(folders_needed), 500):
        chunk = folders_needed[k:k + 500]
        for fid, folder, fn in conn.execute(f'SELECT id, folder, filename FROM images WHERE deleted_at IS NULL '
                                            f'AND folder IN ({",".join("?" * len(chunk))})', chunk):
            if folder not in firsts or _natural(fn) < _natural(firsts[folder][1]):
                firsts[folder] = (fid, fn)
    for a in out:
        if not a['cover_id'] and a['folder'] in firsts:
            a['cover_id'] = firsts[a['folder']][0]
    covers, ids = {}, [a['cover_id'] for a in out if a['cover_id']]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        covers.update({r[0]: tuple(r[1:]) for r in conn.execute(f'SELECT id, mtime, width, height, path, archive, member FROM images '
                                                                 f'WHERE id IN ({",".join("?" * len(chunk))})', chunk)})
    unsized = [(i, c[3], c[4], c[5]) for i, c in covers.items() if not (c[1] and c[2])]
    if unsized and _read_sizes(conn, unsized):                             # the list shows each cover's own shape
        for i, *_rest in unsized:
            covers[i] = covers[i][:1] + tuple(conn.execute('SELECT width, height FROM images WHERE id = ?', (i,)).fetchone()) + covers[i][3:]
    for a in out:
        c = covers.get(a['cover_id']) or (0, None, None)
        a['cover_v'], a['cover_w'], a['cover_h'] = c[0], c[1], c[2]


def album_images(conn, folder):
    rows = conn.execute('SELECT id, filename, width, height, mtime, favorite, size, path FROM images '
                        'WHERE deleted_at IS NULL AND folder = ?', (folder,)).fetchall()
    items = [{'id': r[0], 'filename': r[1], 'width': r[2], 'height': r[3], 'mtime': r[4] or 0,
              'favorite': bool(r[5]), 'size': r[6], 'path': r[7]} for r in rows]
    items.sort(key=lambda i: _natural(i['filename']))
    return items


def image_info(conn, image_id):
    """What the right-click File Info shows for an image; None if it's gone."""
    r = conn.execute('SELECT path, library, folder, size, width, height, mtime, added_at, archive, member FROM images '
                     'WHERE id = ? AND deleted_at IS NULL', (image_id,)).fetchone()
    if not r:
        return None
    return dict(zip(('path', 'library', 'folder', 'size', 'width', 'height', 'mtime', 'added_at', 'archive', 'member'), r))


def album_info(conn, folder):
    """The same for an album; None if it has no images left."""
    r = conn.execute('SELECT COUNT(*), SUM(size), MAX(mtime), MAX(library), MAX(archive) FROM images '
                     'WHERE folder = ? AND deleted_at IS NULL', (folder,)).fetchone()
    if not r or not r[0]:
        return None
    return {'path': folder, 'name': album_title(conn, folder), 'count': r[0], 'size': r[1] or 0, 'mtime': r[2],
            'library': r[3], 'archive': r[4]}


def get(conn, image_id):
    """(path, mtime) of an image that's still there, else None."""
    r = conn.execute('SELECT path, mtime FROM images WHERE id = ? AND deleted_at IS NULL', (image_id,)).fetchone()
    return (r[0], r[1]) if r else None


def archived(conn, image_id):
    """The bytes of an image kept in a zip; None for a plain file, or if it can't be read now."""
    r = conn.execute('SELECT archive, member FROM images WHERE id = ?', (image_id,)).fetchone()
    if not r or not r[0]:
        return None
    try:
        with zipfile.ZipFile(r[0]) as z:
            return z.read(r[1])
    except (OSError, KeyError, RuntimeError, ValueError, NotImplementedError, EOFError, zipfile.BadZipFile) as e:
        print(f'[images] can\'t read {r[1]} in {r[0]}: {e}')
        return None


def library_of(conn, image_id=None, folder=None):
    if image_id is not None:
        r = conn.execute('SELECT library FROM images WHERE id = ?', (image_id,)).fetchone()
    else:
        r = conn.execute('SELECT library FROM images WHERE folder = ? LIMIT 1', (folder,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------------------
# favorites / covers
# ---------------------------------------------------------------------------
def set_favorite(conn, ids=None, folder=None, on=True):
    if folder:
        conn.execute('INSERT INTO image_albums (folder, favorite) VALUES (?, ?) '
                     'ON CONFLICT(folder) DO UPDATE SET favorite = excluded.favorite', (folder, 1 if on else 0))
    for k in range(0, len(ids or []), 500):
        chunk = list(ids[k:k + 500])
        conn.execute(f'UPDATE images SET favorite = ? WHERE id IN ({",".join("?" * len(chunk))})', [1 if on else 0] + chunk)
    conn.commit()


def set_cover(conn, folder, image_id):
    conn.execute('INSERT INTO image_albums (folder, cover_id) VALUES (?, ?) '
                 'ON CONFLICT(folder) DO UPDATE SET cover_id = excluded.cover_id', (folder, image_id))
    conn.commit()


# ---------------------------------------------------------------------------
# album metadata: title, year, rating and the list fields (cast, site, tags, ...)
# ---------------------------------------------------------------------------
_FIELD = re.compile(r'^[a-z][a-z0-9_]{0,39}$')


def album_meta(conn, folders=None):
    """{folder: {field: [values]}} (every album with any, or just these)."""
    out = {}
    if folders is None:
        rows = conn.execute('SELECT folder, field, value FROM image_album_meta').fetchall()
    else:
        folders, rows = list(folders), []
        for k in range(0, len(folders), 500):
            chunk = folders[k:k + 500]
            rows += conn.execute(f'SELECT folder, field, value FROM image_album_meta WHERE folder IN ({",".join("?" * len(chunk))})',
                                 chunk).fetchall()
    for folder, field, value in rows:
        out.setdefault(folder, {}).setdefault(field, []).append(value)
    for m in out.values():
        for vs in m.values():
            vs.sort(key=str.lower)
    return out


def album_title(conn, folder):
    r = conn.execute('SELECT title FROM image_albums WHERE folder = ?', (folder,)).fetchone()
    return (r and r[0]) or title(folder)


def set_album_meta(conn, folders, updates=None, add=None, remove=None):
    """Edit Album, for one album or several at once (the same shape the scene editor saves):
    updates {title / year / rating: text ('' clears)}, add / remove {field: [values]}.
    A value already there in another case isn't added twice."""
    for folder in folders:
        for k, v in (updates or {}).items():
            v = (v or '').strip()
            if k == 'title':
                conn.execute('INSERT INTO image_albums (folder, title) VALUES (?, ?) '
                             'ON CONFLICT(folder) DO UPDATE SET title = excluded.title', (folder, v or None))
            elif k in TEXT_FIELDS:
                conn.execute('DELETE FROM image_album_meta WHERE folder = ? AND field = ?', (folder, k))
                if v:
                    conn.execute('INSERT INTO image_album_meta (folder, field, value) VALUES (?, ?, ?)', (folder, k, v))
        for field, values in (remove or {}).items():
            for v in values or []:
                conn.execute('DELETE FROM image_album_meta WHERE folder = ? AND field = ? AND value = ?', (folder, field, v))
        for field, values in (add or {}).items():
            if not _FIELD.match(field or '') or field in TEXT_FIELDS:
                continue
            have = {r[0].lower() for r in conn.execute('SELECT value FROM image_album_meta WHERE folder = ? AND field = ?',
                                                       (folder, field))}
            for v in values or []:
                v = str(v).strip()
                if v and v.lower() not in have:
                    conn.execute('INSERT INTO image_album_meta (folder, field, value) VALUES (?, ?, ?)', (folder, field, v))
                    have.add(v.lower())
    conn.commit()


# ---------------------------------------------------------------------------
# the videos an album goes with
# ---------------------------------------------------------------------------
def linked_videos(conn, folder):
    """[(video_id, path)] of the album's videos that still have a file (the preferred one first)."""
    rows = conn.execute('SELECT l.video_id, (SELECT path FROM file_versions fv WHERE fv.video_id = l.video_id '
                        'AND fv.deleted_at IS NULL ORDER BY fv.is_preferred DESC LIMIT 1) FROM image_album_videos l '
                        'WHERE l.folder = ? ORDER BY l.rowid', (folder,)).fetchall()
    return [r for r in rows if r[1]]


def link_videos(conn, folder, video_ids, on=True):
    for vid in video_ids or []:
        if on:
            conn.execute('INSERT OR IGNORE INTO image_album_videos (folder, video_id) VALUES (?, ?)', (folder, str(vid)))
        else:
            conn.execute('DELETE FROM image_album_videos WHERE folder = ? AND video_id = ?', (folder, str(vid)))
    conn.commit()


def albums_of_video(conn, video_id):
    """[{folder, name}] of the albums (still with photos) linked to this video."""
    out = []
    for (folder,) in conn.execute('SELECT folder FROM image_album_videos WHERE video_id = ? ORDER BY rowid', (str(video_id),)):
        if conn.execute('SELECT 1 FROM images WHERE folder = ? AND deleted_at IS NULL LIMIT 1', (folder,)).fetchone():
            out.append({'folder': folder, 'name': album_title(conn, folder)})
    return out


def suggest_videos(conn, folder, limit=40):
    """[(video_id, path)]: videos likely to go with this album, not linked yet - in its folder (or a zip's)
    or one below, loose in the folder above it, or in a folder next to it - never above its library folder.
    The ones named most like the album come first."""
    r = conn.execute('SELECT root, archive FROM images WHERE folder = ? AND deleted_at IS NULL LIMIT 1', (folder,)).fetchone()
    if not r:
        return []
    root, archive = r
    base = os.path.dirname(archive) if archive else folder                  # a real folder, not one inside a zip
    parent = os.path.dirname(base)
    inside_root = root and (parent == root or parent.startswith(root.rstrip(os.sep) + os.sep))
    linked = {x[0] for x in conn.execute('SELECT video_id FROM image_album_videos WHERE folder = ?', (folder,))}
    rows = conn.execute("SELECT video_id, path FROM file_versions WHERE deleted_at IS NULL AND path LIKE ? ESCAPE '\\'",
                        (_like_prefix(parent if inside_root else base),)).fetchall()
    seen, out = set(), []
    for vid, path in rows:
        d = os.path.dirname(path)
        near = d == base or os.path.dirname(d) == base or (inside_root and (d == parent or os.path.dirname(d) == parent))
        if near and vid not in linked and vid not in seen:
            seen.add(vid)
            out.append((vid, path))
    name = album_title(conn, folder).lower()
    out.sort(key=lambda x: (not x[1].startswith(base + os.sep),
                            -difflib.SequenceMatcher(None, name, os.path.splitext(os.path.basename(x[1]))[0].lower()).ratio()))
    return out[:limit]


# ---------------------------------------------------------------------------
# photo sizes (the album grid shows each photo whole, so it needs them before the thumbnails load)
# ---------------------------------------------------------------------------
def _upright(im):
    """(width, height) the way the photo is shown: EXIF says 5-8 when it's stored sideways."""
    w, h = im.size
    try:
        if (im.getexif() or {}).get(0x0112) in (5, 6, 7, 8):
            return h, w
    except Exception:
        pass
    return w, h


def _size_of(path):
    from PIL import Image
    try:
        with Image.open(path) as im:                                       # reads the header only
            return _upright(im)
    except Exception:
        return None


def fill_sizes(conn, folder, limit=None):
    """Read width / height for the album's photos that don't have them yet (once per photo), the first
    `limit` in name order (None: all). -> how many were read. ~3 ms a photo on a network share."""
    rows = conn.execute('SELECT id, path, archive, member, filename FROM images WHERE folder = ? AND deleted_at IS NULL '
                        'AND (width IS NULL OR height IS NULL)', (folder,)).fetchall()
    return _read_sizes(conn, [r[:4] for r in sorted(rows, key=lambda r: _natural(r[4]))][:limit])


def _read_sizes(conn, rows):
    """rows: (id, path, archive, member) -> width / height stored; how many could be read."""
    if not rows:
        return 0
    sizes = {}
    plain = [(i, p) for i, p, a, _m in rows if not a]
    if plain:
        with ThreadPoolExecutor(max_workers=8) as pool:                     # a network share answers faster in parallel
            for (i, _p), s in zip(plain, pool.map(lambda x: _size_of(x[1]), plain)):
                sizes[i] = s
    zips = {}
    for i, _p, a, m in rows:
        if a:
            zips.setdefault(a, []).append((i, m))
    for a, members in zips.items():
        try:
            from PIL import Image
            with zipfile.ZipFile(a) as z:
                for i, m in members:
                    try:
                        with z.open(m) as f, Image.open(f) as im:          # seekable: only the header is read
                            sizes[i] = _upright(im)
                    except Exception:
                        sizes[i] = None
        except (OSError, zipfile.BadZipFile, ValueError) as e:
            print(f'[images] can\'t read {a}: {e}')
    n = 0
    for i, s in sizes.items():
        if s:
            conn.execute('UPDATE images SET width = ?, height = ? WHERE id = ?', (s[0], s[1], i))
            n += 1
    conn.commit()
    return n


_filling = set()


def fill_sizes_later(get_conn, folder):
    """The rest of a big album in the background (its own connection); one run per album at a time."""
    if folder in _filling:
        return
    _filling.add(folder)

    def run():
        conn = None
        try:
            conn = get_conn()
            fill_sizes(conn, folder)
        except Exception as e:
            print(f'[images] sizes for {folder}: {e}')
        finally:
            _filling.discard(folder)
            if conn is not None:
                conn.close()
    threading.Thread(target=run, daemon=True, name='image-sizes').start()


def sizes(conn, folder):
    """{id: [width, height]} of the album's photos whose size is known (the grid catching up)."""
    return {r[0]: [r[1], r[2]] for r in conn.execute('SELECT id, width, height FROM images WHERE folder = ? AND deleted_at IS NULL '
                                                     'AND width IS NOT NULL AND height IS NOT NULL', (folder,))}


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------
def thumb(conn, image_id, cache_dir):
    """A WebP thumbnail's file path (made on first use), or None (gone / not an image Pillow can read)."""
    g = get(conn, image_id)
    if not g:
        return None
    path, mtime = g
    out_dir = os.path.join(cache_dir, 'images')
    out = os.path.join(out_dir, f'{image_id}_{mtime or 0}.webp')
    if os.path.exists(out):
        return out
    with _thumb_slots:
        if os.path.exists(out):                                            # made while this one waited
            return out
        try:
            from PIL import Image, ImageOps
            data = archived(conn, image_id)
            with Image.open(path if data is None else io.BytesIO(data)) as im:
                w, h = _upright(im)
                if im.format == 'JPEG':
                    im.draft('RGB', (THUMB_SIZE * 2, THUMB_SIZE * 2))              # decode at a fraction of the size: much faster
                im = ImageOps.exif_transpose(im)
                if im.mode not in ('RGB', 'RGBA'):
                    im = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                os.makedirs(out_dir, exist_ok=True)
                tmp = out + '.tmp'
                im.save(tmp, 'WEBP', quality=80)
                os.replace(tmp, out)
        except Exception as e:
            print(f'[images] thumbnail failed for {path}: {e}')
            return None
    conn.execute('UPDATE images SET width = ?, height = ? WHERE id = ?', (w, h, image_id))
    conn.commit()
    return out
