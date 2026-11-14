"""
Pictures kept as files instead of inside the database: movie covers (front and back) and performer
photos (the photo and its uncropped original). Small, often-read pictures - performer thumbnails,
scene thumbnails - stay in the database.

The database column keeps a short reference, 'store:ab/abcdef….jpg', in place of the picture's
bytes; the file is <data>/photos/ab/abcdef….jpg, named by its SHA-256 (the same picture is stored
once, a rename never moves a file). read() takes either a reference or bytes, so rows that haven't
been moved yet keep working.

Why: 1.8 GB of pictures in a 2.2 GB database made every backup (one per start) 2.2 GB, and every
query over the movies table read through its covers.

migrate() moves the pictures out (in the background at start-up, in small batches) and then
compacts the database once. gc() removes files nothing refers to any more, only when they're
older than GC_DAYS, so a database backup from the last few weeks still finds its pictures.
"""
import hashlib
import logging
import os
import sqlite3
import threading
import time

from config import DATA_DIR

log = logging.getLogger('lustr.photo_store')

STORE_DIR = os.path.join(DATA_DIR, 'photos')
PREFIX = 'store:'
GC_DAYS = 60
# (table, key column, picture column) - every place a stored picture can be referenced
COLUMNS = (('movies', 'id', 'cover'), ('movies', 'id', 'back_cover'),
           ('cast_photos', 'cast_name', 'photo_data'), ('cast_photos', 'cast_name', 'original_data'))
_EXT = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif', 'image/avif': '.avif',
        'image/bmp': '.bmp'}


def _sniff(data):
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data[:3] == b'GIF':
        return '.gif'
    return '.img'


def is_ref(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def _path(ref):
    rel = ref[len(PREFIX):]
    if '..' in rel or rel.startswith('/'):
        raise ValueError('bad picture reference')
    return os.path.join(STORE_DIR, rel)


def put(data, mime=None):
    """Store a picture -> its reference (None for no picture)."""
    if data is None:
        return None
    if is_ref(data):
        return data
    data = bytes(data)
    if not data:
        return None
    digest = hashlib.sha256(data).hexdigest()
    rel = f'{digest[:2]}/{digest}{_EXT.get((mime or "").lower()) or _sniff(data)}'
    full = os.path.join(STORE_DIR, rel)
    if not os.path.exists(full):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = f'{full}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, full)
    else:
        try:
            os.utime(full)                     # in use again: gc() counts age from here
        except OSError:
            pass
    return PREFIX + rel


def read(value):
    """A column's value (a reference, or the bytes of a row not moved yet) -> the picture's bytes, or None."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value) or None
    if is_ref(value):
        try:
            with open(_path(value), 'rb') as f:
                return f.read()
        except (OSError, ValueError):
            log.warning(f'picture file missing: {value}')
            return None
    return None


# ---------- moving pictures out of the database ----------
def _exists(conn, table, col):
    try:
        return col in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
    except sqlite3.DatabaseError:
        return False


def pending(conn):
    """How many pictures are still inside the database."""
    n = 0
    for table, _key, col in COLUMNS:
        if _exists(conn, table, col):
            n += conn.execute(f"SELECT COUNT(*) FROM {table} WHERE typeof({col}) = 'blob'").fetchone()[0]
    return n


def migrate(conn, batch=50, pause=0.05, stop=None):
    """Move every picture still in the database into files, a batch per transaction (the app stays
    usable meanwhile). -> pictures moved."""
    moved = 0
    for table, key, col in COLUMNS:
        if not _exists(conn, table, col):
            continue
        mime_col = {'cover': 'cover_mime', 'back_cover': 'back_cover_mime'}.get(col, 'photo_mime_type')
        if not _exists(conn, table, mime_col):
            mime_col = 'NULL'
        while not (stop and stop()):
            rows = conn.execute(f"SELECT {key}, {col}, {mime_col} FROM {table} WHERE typeof({col}) = 'blob' LIMIT ?",
                                (batch,)).fetchall()
            if not rows:
                break
            for k, data, mime in rows:
                ref = put(data, mime if col != 'original_data' else None) if data else None
                conn.execute(f'UPDATE {table} SET {col} = ? WHERE {key} = ?', (ref, k))
            conn.commit()
            moved += len(rows)
            time.sleep(pause)
    return moved


def compact(conn):
    """Give the space the pictures took back to the disk (VACUUM). The app waits a few seconds."""
    started = time.time()
    before = os.path.getsize(_db_path(conn))
    conn.commit()
    conn.execute('VACUUM')
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except sqlite3.DatabaseError:
        pass
    after = os.path.getsize(_db_path(conn))
    log.info(f'Database compacted: {before / 1e6:,.0f} MB -> {after / 1e6:,.0f} MB in {time.time() - started:.0f}s')


def _db_path(conn):
    return conn.execute('PRAGMA database_list').fetchone()[2]


def migrate_background(get_conn):
    """At start-up: move pictures out of the database, then compact it once."""
    def run():
        try:
            conn = get_conn()
            n = pending(conn)
            if not n:
                return
            log.info(f'Moving {n:,} pictures (movie covers, performer photos) out of the database into {STORE_DIR}')
            started = time.time()
            moved = migrate(conn)
            log.info(f'Moved {moved:,} pictures in {time.time() - started:.0f}s')
            free, total = (conn.execute(f'PRAGMA {p}').fetchone()[0] for p in ('freelist_count', 'page_count'))
            if total and free / total > 0.25:
                compact(conn)
        except Exception as e:
            log.error(f'Moving pictures out of the database stopped (it continues on the next start): {e}')
    threading.Thread(target=run, name='photo-store', daemon=True).start()


# ---------- clean-up ----------
def referenced(conn):
    refs = set()
    for table, _key, col in COLUMNS:
        if _exists(conn, table, col):
            refs.update(r[0] for r in conn.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE '{PREFIX}%'"))
    return refs


def gc(conn, dry_run=False, days=GC_DAYS):
    """Delete picture files nothing refers to that haven't been used for `days` days. -> (files, bytes)."""
    if not os.path.isdir(STORE_DIR):
        return 0, 0
    keep = {_path(r) for r in referenced(conn)}
    cutoff = time.time() - days * 86400
    n = size = 0
    for sub in os.listdir(STORE_DIR):
        folder = os.path.join(STORE_DIR, sub)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if full in keep or st.st_mtime > cutoff:
                continue
            n += 1
            size += st.st_size
            if not dry_run:
                try:
                    os.remove(full)
                except OSError:
                    pass
    return n, size


def disk_usage():
    n = size = 0
    for root, _dirs, files in os.walk(STORE_DIR):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n, size
