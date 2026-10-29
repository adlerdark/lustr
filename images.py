# This file is: ./images.py
"""
Images in your library folders: an Images tab per library, albums by folder, a viewer.

The library scan (model.scan_library) hands over every image file it walked past; sync() keeps the
`images` table in step (new / gone / back). Folders named like screenshot folders (skip_folders,
Settings -> Browsing) are left out when listing, so changing that list shows at once.
Thumbnails are made on first view (Pillow) and kept in CACHE_DIR/images.

Browse and view only for now: favorites, a chosen album cover. No tags / cast.
No FastAPI imports.
"""
import json
import os
import re
import threading
from datetime import datetime

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
SKIP_KEY = 'image_skip_folders'
DEFAULT_SKIP = ['screens', 'screen', 'thumbnails', 'thumbs', 'screenshots']
THUMB_SIZE = 480
TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
         '.gif': 'image/gif', '.bmp': 'image/bmp', '.avif': 'image/avif'}

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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_images_lib_folder ON images(library, folder)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_images_deleted ON images(deleted_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS image_albums (
        folder TEXT PRIMARY KEY,
        favorite INTEGER DEFAULT 0,
        cover_id INTEGER)''')
    conn.commit()


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def sync(conn, library, roots, paths):
    """After a scan: `paths` = the images now found under `roots` (the library's roots that were
    walked). New ones are added, ones no longer there are marked gone, ones back are restored.
    Roots that weren't walked (an offline share) are left alone. -> {added, removed, restored}"""
    ensure_schema(conn)
    roots = [os.path.normpath(r) for r in roots]
    found = {os.path.normpath(p) for p in paths}
    now = datetime.now().isoformat()
    under = ' OR '.join(['path LIKE ? ESCAPE \'\\\''] * len(roots)) or '0'
    likes = [_like_prefix(r) for r in roots]
    known = {r[0]: r[1] for r in conn.execute(f'SELECT path, deleted_at FROM images WHERE {under}', likes)}
    added = restored = removed = 0
    for p in found:
        if p not in known:
            try:
                st = os.stat(p)
            except OSError:
                continue
            root = max((r for r in roots if p.startswith(r + os.sep)), key=len, default=None)
            conn.execute('INSERT OR IGNORE INTO images (path, library, root, folder, filename, size, mtime, added_at) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (p, library, root, os.path.dirname(p), os.path.basename(p), st.st_size, int(st.st_mtime), now))
            added += 1
        elif known[p] is not None:
            conn.execute('UPDATE images SET deleted_at = NULL, library = ? WHERE path = ?', (library, p))
            restored += 1
    gone = [p for p, d in known.items() if d is None and p not in found]
    for k in range(0, len(gone), 500):
        chunk = gone[k:k + 500]
        removed += conn.execute(f'UPDATE images SET deleted_at = ? WHERE path IN ({",".join("?" * len(chunk))})',
                                [now] + chunk).rowcount
    conn.commit()
    return {'added': added, 'removed': removed, 'restored': restored}


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


def counts(conn, libraries):
    """{library: images shown} (skipped folders left out)."""
    out = {}
    for f in _folders(conn, libraries).values():
        out[f['library']] = out.get(f['library'], 0) + f['count']
    return out


def albums(conn, libraries, sort='name', q=''):
    folders = _folders(conn, libraries)
    if not folders:
        return []
    meta = {r[0]: (r[1], r[2]) for r in conn.execute('SELECT folder, favorite, cover_id FROM image_albums')}
    base_count = {}
    for f in folders:
        b = os.path.basename(f).lower()
        base_count[b] = base_count.get(b, 0) + 1
    out = []
    ql = (q or '').strip().lower()
    for f, d in folders.items():
        base = os.path.basename(f)
        name = base
        if base_count[base.lower()] > 1 and f != d['root']:                 # several 'Images' folders: 'Set / Images'
            name = f'{os.path.basename(os.path.dirname(f))} / {base}'
        if ql and ql not in f.lower():
            continue
        fav, cover = meta.get(f, (0, None))
        out.append({'folder': f, 'name': name, 'library': d['library'], 'count': d['count'], 'newest': d['newest'],
                    'favorite': bool(fav), 'cover_id': cover})
    # covers: the chosen one if it's still there, else the first image by name
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
        covers.update(conn.execute(f'SELECT id, mtime FROM images WHERE id IN ({",".join("?" * len(chunk))})', chunk).fetchall())
    for a in out:
        a['cover_v'] = covers.get(a['cover_id'], 0)
    keys = {'newest': lambda a: (-a['newest'], _natural(a['name'])),
            'count': lambda a: (-a['count'], _natural(a['name'])),
            'favorites': lambda a: (not a['favorite'], _natural(a['name']))}
    out.sort(key=keys.get(sort, lambda a: _natural(a['name'])))
    return out


def album_images(conn, folder):
    rows = conn.execute('SELECT id, filename, width, height, mtime, favorite, size FROM images '
                        'WHERE deleted_at IS NULL AND folder = ?', (folder,)).fetchall()
    items = [{'id': r[0], 'filename': r[1], 'width': r[2], 'height': r[3], 'mtime': r[4] or 0,
              'favorite': bool(r[5]), 'size': r[6]} for r in rows]
    items.sort(key=lambda i: _natural(i['filename']))
    return items


def get(conn, image_id):
    """(path, mtime) of an image that's still there, else None."""
    r = conn.execute('SELECT path, mtime FROM images WHERE id = ? AND deleted_at IS NULL', (image_id,)).fetchone()
    return (r[0], r[1]) if r else None


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
            with Image.open(path) as im:
                w, h = im.size
                if (im.getexif() or {}).get(0x0112) in (5, 6, 7, 8):                # stored sideways: upright size
                    w, h = h, w
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
