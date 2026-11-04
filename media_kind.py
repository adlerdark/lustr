# This file is: ./media_kind.py
"""Which tab a file shows in: the video library or Images.

- Photos (images.IMAGE_EXTENSIONS) are always Images.
- A library can hold only images or only videos (library setting 'content': 'both' | 'images' | 'videos').
- Otherwise a video file is a video, except .webm (ANIMATED): short loops that play like a GIF, so Images.
- You can switch any one video file either way (table media_kind: your choice per path)."""
import os

from config import VIDEO_EXTENSIONS

ANIMATED = {'.webm'}
CONTENTS = ('both', 'images', 'videos')


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS media_kind (path TEXT PRIMARY KEY, kind TEXT NOT NULL)')


def is_moving(name):
    """A video file type (it can show in either tab)."""
    return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS


def content_of(model, library):
    c = ((getattr(model, 'library_settings', None) or {}).get(library) or {}).get('content')
    return c if c in CONTENTS else 'both'


def overrides(conn):
    ensure_schema(conn)
    return dict(conn.execute('SELECT path, kind FROM media_kind').fetchall())


def kind_of(path, chosen=None, content='both'):
    """'image' / 'video' for a video file type, None for anything else."""
    if not is_moving(path):
        return None
    if content in ('images', 'videos'):
        return content[:-1]
    k = (chosen or {}).get(path)
    if k in ('image', 'video'):
        return k
    return 'image' if os.path.splitext(path)[1].lower() in ANIMATED else 'video'


def set_kind(conn, paths, kind):
    ensure_schema(conn)
    conn.executemany('INSERT OR REPLACE INTO media_kind (path, kind) VALUES (?, ?)', [(p, kind) for p in paths])
    conn.commit()


def has_metadata(conn, video_id):
    """Anything you'd lose by removing the video: values, a movie or scene link, favorite, rating."""
    r = conn.execute('SELECT favorite, COALESCE(rating, \'\') FROM videos WHERE video_id = ?', (video_id,)).fetchone()
    if r and (r[0] or str(r[1]).strip()):
        return True
    tables = [t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND "
                                         "(name LIKE 'video\\_%\\_junction' ESCAPE '\\' OR name IN ('video_movie', 'video_ext_link'))")]
    return any(conn.execute(f'SELECT 1 FROM {t} WHERE video_id = ? LIMIT 1', (video_id,)).fetchone() for t in tables)


def would_lose(conn, paths):
    """Of these video files: how many are the last file of a video that has metadata."""
    n = 0
    for p in paths:
        r = conn.execute('SELECT video_id FROM file_versions WHERE path = ? AND deleted_at IS NULL', (p,)).fetchone()
        if not r:
            continue
        others = conn.execute('SELECT COUNT(*) FROM file_versions WHERE video_id = ? AND path != ? AND deleted_at IS NULL',
                              (r[0], p)).fetchone()[0]
        n += not others and has_metadata(conn, r[0])
    return n


# ---------------------------------------------------------------------------
# moving files between the tabs
# ---------------------------------------------------------------------------
def _roots_of(model, library):
    return (getattr(model, 'libraries', None) or {}).get(library) or []


def to_images(model, paths):
    """Video files -> Images: their video entries go (a video's metadata with its last file), image rows come.
    -> how many are in Images now."""
    import images
    conn = model.db.get_connection()
    images.ensure_schema(conn)
    by_lib = {}
    for p in paths:
        lib = model.db._get_library_for_path(p)
        if conn.execute('SELECT 1 FROM file_versions WHERE path = ?', (p,)).fetchone():
            model.db.remove_file_version(p)
        by_lib.setdefault(lib, []).append(p)
    conn.commit()
    return sum(images.add_files(conn, lib, _roots_of(model, lib), ps) for lib, ps in by_lib.items())


def to_videos(model, paths):
    """Image rows of video files -> the video library, as new videos (thumbnails made in the background)."""
    import threading
    import images
    from media_handler import MediaHandler
    conn = model.db.get_connection()
    images.remove_files(conn, paths)
    added = []
    for p in paths:
        if os.path.exists(p) and not conn.execute('SELECT 1 FROM file_versions WHERE path = ? AND deleted_at IS NULL', (p,)).fetchone():
            model._ensure_file_entry(p, os.path.basename(p))
            added.append(p)

    def thumbs():
        for p in added:
            try:
                MediaHandler.generate_thumbnails(p)
            except Exception as e:
                print(f'[media kind] thumbnails for {p}: {e}')
    if added:
        threading.Thread(target=thumbs, daemon=True, name='media-kind-thumbs').start()
    return len(added)


def migrate(model):
    """At start-up: video entries that belong in Images now (a .webm; files in an images-only library) move
    there, unless that would lose metadata - those stay videos (and are remembered as videos)."""
    conn = model.db.get_connection()
    chosen = overrides(conn)
    rows = conn.execute('SELECT fv.path, COALESCE(fv.library, v.library) FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                        'WHERE fv.deleted_at IS NULL').fetchall()
    move, keep = [], []
    for path, lib in rows:
        if kind_of(path, chosen, content_of(model, lib)) == 'image':
            (keep if would_lose(conn, [path]) and content_of(model, lib) != 'images' else move).append(path)
    if keep:
        set_kind(conn, keep, 'video')
    if move:
        to_images(model, move)
    if move or keep:
        print(f'[media kind] {len(move)} files moved to Images, {len(keep)} with metadata kept as videos')
    return len(move), len(keep)
