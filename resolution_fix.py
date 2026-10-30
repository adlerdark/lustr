# This file is: ./resolution_fix.py
"""
Fix resolutions (Settings -> Maintenance): files read before resolutions meant the short side have
their long side stored, so a vertical 1080x1920 says 1920p. Width was never stored, so every file is
re-read once.

Only a value the old rule produced for that file is replaced: one typed in by hand stays, and so
does a file that can't be read. Running it again changes nothing.

No FastAPI imports.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from media_handler import MediaHandler

WORKERS = 4
BATCH = 200                     # files read between saves

_job = {'running': False, 'done': 0, 'total': 0, 'changed': 0, 'unreadable': 0, 'error': '', 'backup': None, 'finished': False}
_lock = threading.Lock()


def _old_label(width, height):
    if height > 0:
        return '4K' if width >= 3800 else f'{height}p'
    return f'{width}w' if width > 0 else ''


def correction(stored, width, height):
    """-> the value to store instead, or None to leave this file alone."""
    if not (width and height) or stored != _old_label(width, height):
        return None
    new = MediaHandler.resolution_label(width, height)
    return new if new != stored else None


def _read(row):
    path, stored = row
    meta = MediaHandler.get_extended_metadata(path)
    return path, stored, meta.get('width') or 0, meta.get('height') or 0


def run(conn, read=_read, progress=None):
    """Correct every active file -> {changed, unreadable}."""
    rows = conn.execute("SELECT path, resolution FROM file_versions "
                        "WHERE deleted_at IS NULL AND resolution IS NOT NULL AND resolution != ''").fetchall()
    rows = [(r[0], r[1]) for r in rows]
    changed = unreadable = 0
    if progress:
        progress(total=len(rows))
    with ThreadPoolExecutor(WORKERS) as pool:
        for n, (path, stored, width, height) in enumerate(pool.map(read, rows), 1):
            new = correction(stored, width, height)
            if not (width and height):
                unreadable += 1
            elif new:
                changed += conn.execute('UPDATE file_versions SET resolution = ? WHERE path = ? AND resolution = ?',
                                        (new, path, stored)).rowcount
            if n % BATCH == 0:
                conn.commit()
                if progress:
                    progress(done=n, changed=changed, unreadable=unreadable)
    conn.commit()
    if progress:
        progress(done=len(rows), changed=changed, unreadable=unreadable)
    return {'changed': changed, 'unreadable': unreadable}


def status():
    return dict(_job)


def start(model):
    """Back up, then run in the background -> the job's status."""
    with _lock:
        if _job['running']:
            return status()
        _job.update(running=True, done=0, total=0, changed=0, unreadable=0, error='', backup=None, finished=False)
    threading.Thread(target=_worker, args=(model,), name='resolution-fix', daemon=True).start()
    return status()


def _worker(model):
    import db_maintenance as dm
    try:
        conn = model.db.get_connection()
        _job['backup'] = dm.make_backup(conn)
        run(conn, progress=lambda **kw: _job.update(kw))
        _job['finished'] = True
    except Exception as e:
        _job['error'] = str(e)
        print(f"[resolution-fix] failed: {e}")
    finally:
        _job['running'] = False
        try:
            from routes import videos
            videos.clear_filter_caches()
        except Exception:
            pass
