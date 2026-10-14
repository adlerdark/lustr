# This file is: ./chapters.py
"""
Chapters embedded in video files (MKV, some MP4s) become markers: a point marker at each
chapter's start, titled with the chapter's name.

Each file is checked once (chapter_checks: path, size, mtime) - again only when the file changes.
Nothing you do to a marker is undone and nothing is duplicated:
  * a chapter marker keeps its source_key when you rename / move / retag it, so it isn't imported again;
  * deleting one records its key (marker_dismissed), so it doesn't come back;
  * a chapter within 1.5 s of any marker the video already has is skipped.
No FastAPI imports.
"""
import json
import os
import re
import subprocess
import threading
import time

import transcoder

NEAR = 1.5                # seconds: a marker this close to a chapter start counts as the same place


def ensure_schema(conn):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(video_markers)')}
    if cols and 'source' not in cols:
        conn.execute("ALTER TABLE video_markers ADD COLUMN source TEXT DEFAULT 'user'")
    if cols and 'source_key' not in cols:
        conn.execute('ALTER TABLE video_markers ADD COLUMN source_key TEXT')
    conn.execute('CREATE TABLE IF NOT EXISTS chapter_checks (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, '
                 'chapters INTEGER, checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS marker_dismissed (video_id TEXT NOT NULL, source_key TEXT NOT NULL, '
                 'PRIMARY KEY (video_id, source_key))')


def read(path):
    """-> [(start seconds, title)]; [] when the file has no chapters, or only one (the whole file)."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_chapters', '-of', 'json', path],
                           capture_output=True, text=True, timeout=60, env=transcoder.ENV)
        chs = json.loads(r.stdout or '{}').get('chapters') or []
    except Exception:
        return []
    out = []
    for i, c in enumerate(chs, 1):
        try:
            start = max(0.0, float(c.get('start_time')))
        except (TypeError, ValueError):
            continue
        title = re.sub(r'\s+', ' ', str((c.get('tags') or {}).get('title') or '')).strip()[:120] or f'Chapter {i}'
        out.append((round(start, 3), title))
    return out if len(out) > 1 else []


def _sig(path):
    st = os.stat(path)
    return st.st_size, int(st.st_mtime)


def checked(conn, path):
    try:
        size, mtime = _sig(path)
    except OSError:
        return True                                   # gone: nothing to check
    row = conn.execute('SELECT size, mtime FROM chapter_checks WHERE path = ?', (path,)).fetchone()
    return bool(row) and row[0] == size and row[1] == mtime


def key(start, index):
    return f'chapter:{int(round(start * 1000))}:{index}'


def import_file(conn, path, force=False):
    """Chapters of one file -> markers on its video. -> number of markers added"""
    ensure_schema(conn)
    if not force and checked(conn, path):
        return 0
    row = conn.execute('SELECT video_id FROM file_versions WHERE path = ? AND deleted_at IS NULL', (path,)).fetchone()
    try:
        size, mtime = _sig(path)
    except OSError:
        return 0
    chs = read(path) if row else []
    added = 0
    if row:
        vid = row[0]
        keys = {r[0] for r in conn.execute('SELECT source_key FROM video_markers WHERE video_id = ? AND source_key IS NOT NULL', (vid,))}
        keys |= {r[0] for r in conn.execute('SELECT source_key FROM marker_dismissed WHERE video_id = ?', (vid,))}
        times = [r[0] for r in conn.execute('SELECT seconds FROM video_markers WHERE video_id = ?', (vid,))]
        for i, (start, title) in enumerate(chs, 1):
            k = key(start, i)
            if k in keys or any(abs(t - start) <= NEAR for t in times):
                continue
            conn.execute("INSERT INTO video_markers (video_id, seconds, end_seconds, title, tag, source, source_key) "
                         "VALUES (?, ?, NULL, ?, '', 'chapter', ?)", (vid, start, title, k))
            times.append(start)
            keys.add(k)
            added += 1
    conn.execute('INSERT INTO chapter_checks (path, size, mtime, chapters, checked_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, chapters = excluded.chapters, '
                 'checked_at = CURRENT_TIMESTAMP', (path, size, mtime, len(chs)))
    conn.commit()
    return added


def dismiss(conn, marker_id):
    """A chapter marker you delete stays deleted (call before deleting it)."""
    ensure_schema(conn)
    r = conn.execute("SELECT video_id, source_key FROM video_markers WHERE id = ? AND source = 'chapter'", (marker_id,)).fetchone()
    if r and r[1]:
        conn.execute('INSERT OR IGNORE INTO marker_dismissed (video_id, source_key) VALUES (?, ?)', (r[0], r[1]))


def import_paths_background(conn_fn, paths):
    """New files from a scan: check them in a background thread."""
    def run():
        conn, n = conn_fn(), 0
        for p in paths:
            try:
                n += import_file(conn, p)
            except Exception as e:
                print(f'[chapters] {os.path.basename(p)}: {e}')
        if n:
            print(f'[chapters] {n} marker(s) from chapters in {len(paths)} new file(s)')
    threading.Thread(target=run, name='chapters-new', daemon=True).start()


class BulkJob:
    """Settings -> 'Import chapters from all videos': every live file not checked yet, one at a time."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {'running': False, 'total': 0, 'done': 0, 'files_with_chapters': 0, 'markers': 0, 'stop': False,
                      'last_error': ''}

    def status(self):
        with self.lock:
            return dict(self.state)

    def stop(self):
        with self.lock:
            self.state['stop'] = True

    def start(self, conn_fn):
        with self.lock:
            if self.state['running']:
                return False
            self.state.update(running=True, total=0, done=0, files_with_chapters=0, markers=0, stop=False, last_error='')
        threading.Thread(target=self._run, args=(conn_fn,), name='chapters-bulk', daemon=True).start()
        return True

    def _run(self, conn_fn):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except Exception:
            pass
        conn = conn_fn()
        try:
            ensure_schema(conn)
            paths = [r[0] for r in conn.execute('SELECT fv.path FROM file_versions fv LEFT JOIN chapter_checks c ON c.path = fv.path '
                                                'WHERE fv.deleted_at IS NULL AND c.path IS NULL ORDER BY fv.path')]
            self.state['total'] = len(paths)
            for p in paths:
                if self.state['stop']:
                    break
                try:
                    n = import_file(conn, p)
                    if n:
                        self.state['files_with_chapters'] += 1
                        self.state['markers'] += n
                except Exception as e:
                    self.state['last_error'] = f'{os.path.basename(p)}: {e}'
                self.state['done'] += 1
                time.sleep(0.01)
        finally:
            with self.lock:
                self.state['running'] = False


bulk = BulkJob()
