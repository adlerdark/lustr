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


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def counts(conn, video_ids=None):
    """-> ({video_id: chapters in its files}, {video_id: markers - yours and those made from chapters}).
    Chapters: the most any live file of the video has; a video none of whose files was checked is left out."""
    ch, mk = {}, {}
    vids = list(video_ids) if video_ids is not None else None
    chunks = [vids[k:k + 500] for k in range(0, len(vids), 500)] if vids is not None else [None]
    for chunk in chunks:
        if chunk == []:
            continue
        where, params = ('', []) if chunk is None else (f' AND fv.video_id IN ({",".join("?" * len(chunk))})', chunk)
        if _has_table(conn, 'chapter_checks'):
            for r in conn.execute('SELECT fv.video_id, MAX(c.chapters) FROM file_versions fv JOIN chapter_checks c ON c.path = fv.path '
                                  f'WHERE fv.deleted_at IS NULL{where} GROUP BY fv.video_id', params):
                ch[r[0]] = r[1] or 0
        if _markers_ready(conn):
            mwhere = where.replace('fv.video_id', 'video_id')
            for r in conn.execute(f"SELECT video_id, COUNT(*) FROM video_markers WHERE 1{mwhere} GROUP BY video_id", params):
                mk[r[0]] = r[1]
    return ch, mk


def add_info(conn, items):
    """Put chapters (None = file not checked yet) / markers on list items (dicts with video_id)."""
    ch, mk = counts(conn, {i.get('video_id') for i in items if i.get('video_id')})
    for i in items:
        vid = i.get('video_id')
        i['chapters'] = ch.get(vid)
        i['markers'] = mk.get(vid, 0)
    return items


def _markers_ready(conn):
    return _has_table(conn, 'video_markers') and 'source' in {r[1] for r in conn.execute('PRAGMA table_info(video_markers)')}


def filter_sql(conn, kind, has, col='v.video_id'):
    """chapters:yes|no (kind 'file') / markers:yes|no (kind 'markers', any marker) -> SQL condition on col."""
    ensure_schema(conn)
    if kind == 'markers':
        sub = 'SELECT video_id FROM video_markers' if _markers_ready(conn) else 'SELECT NULL WHERE 0'
    else:
        sub = ('SELECT fv2.video_id FROM file_versions fv2 JOIN chapter_checks c ON c.path = fv2.path '
               'WHERE fv2.deleted_at IS NULL AND c.chapters > 0')
    return f"{col} {'IN' if has else 'NOT IN'} ({sub})"


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


def paths_for(conn, libraries=None, paths=None):
    """The live files to check: those of some libraries, some chosen files, or (neither) every file not checked yet."""
    if paths:
        live = set()
        for k in range(0, len(paths), 500):
            chunk = list(paths[k:k + 500])
            live |= {r[0] for r in conn.execute(f'SELECT path FROM file_versions WHERE deleted_at IS NULL '
                                                f'AND path IN ({",".join("?" * len(chunk))})', chunk)}
        return sorted(live)
    if libraries:
        return [r[0] for r in conn.execute(
            'SELECT fv.path FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id WHERE fv.deleted_at IS NULL '
            f'AND v.library IN ({",".join("?" * len(libraries))}) ORDER BY fv.path', list(libraries))]
    return [r[0] for r in conn.execute('SELECT fv.path FROM file_versions fv LEFT JOIN chapter_checks c ON c.path = fv.path '
                                       'WHERE fv.deleted_at IS NULL AND c.path IS NULL ORDER BY fv.path')]


class BulkJob:
    """Import chapters, one file at a time: every live file not checked yet (Settings), a library's files
    or chosen files (a file already checked and unchanged since is skipped quickly)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {'running': False, 'total': 0, 'done': 0, 'files_with_chapters': 0, 'markers': 0, 'stop': False,
                      'last_error': '', 'label': ''}

    def status(self):
        with self.lock:
            return dict(self.state)

    def stop(self):
        with self.lock:
            self.state['stop'] = True

    def start(self, conn_fn, libraries=None, paths=None, label=''):
        with self.lock:
            if self.state['running']:
                return False
            self.state.update(running=True, total=0, done=0, files_with_chapters=0, markers=0, stop=False, last_error='',
                              label=label or ('All videos' if not (libraries or paths) else ''))
        threading.Thread(target=self._run, args=(conn_fn, libraries, paths), name='chapters-bulk', daemon=True).start()
        return True

    def _run(self, conn_fn, libraries=None, paths=None):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except Exception:
            pass
        conn = conn_fn()
        try:
            ensure_schema(conn)
            paths = paths_for(conn, libraries, paths)
            self.state['total'] = len(paths)
            for p in paths:
                if self.state['stop']:
                    break
                try:
                    self.state['markers'] += import_file(conn, p)
                    row = conn.execute('SELECT chapters FROM chapter_checks WHERE path = ?', (p,)).fetchone()
                    if row and row[0]:
                        self.state['files_with_chapters'] += 1
                except Exception as e:
                    self.state['last_error'] = f'{os.path.basename(p)}: {e}'
                self.state['done'] += 1
                time.sleep(0.01)
        finally:
            with self.lock:
                self.state['running'] = False


bulk = BulkJob()
