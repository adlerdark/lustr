# This file is: ./corrupt.py
"""Videos marked as corrupt: the tag corrupt.file. They are hidden from every library view (for everyone;
display_filter.shown_sql leaves them out) and listed in Settings -> Maintenance -> Corrupt files, where
they can be unmarked. A tag is on a video, so the mark covers all of its files.

No FastAPI imports."""
import os

TAG = 'corrupt.file'


def tag_id(conn):
    try:
        r = conn.execute('SELECT id FROM video_tags WHERE LOWER(value) = ? ORDER BY id LIMIT 1', (TAG,)).fetchone()
    except Exception:                  # no tags table yet
        return None
    return r[0] if r else None


def hidden_sql(conn, col='v.video_id'):
    """(sql, params) true for a video that isn't marked, or ('', []) when nothing is."""
    tid = tag_id(conn)
    if tid is None:
        return '', []
    return f'NOT EXISTS (SELECT 1 FROM video_tags_junction cfj WHERE cfj.video_id = {col} AND cfj.tags_id = ?)', [tid]


def count(conn):
    tid = tag_id(conn)
    if tid is None:
        return 0
    return conn.execute('SELECT COUNT(*) FROM video_tags_junction WHERE tags_id = ?', (tid,)).fetchone()[0]


def listing(conn):
    """Every marked video: [{video_id, title, library, paths: [{path, size}]}], by title."""
    tid = tag_id(conn)
    if tid is None:
        return []
    out = {}
    for vid, title, lib, path, size in conn.execute(
            'SELECT v.video_id, v.title, v.library, fv.path, fv.file_size FROM video_tags_junction j '
            'JOIN videos v ON v.video_id = j.video_id '
            'LEFT JOIN file_versions fv ON fv.video_id = v.video_id AND fv.deleted_at IS NULL '
            'WHERE j.tags_id = ? ORDER BY fv.is_preferred DESC', (tid,)):
        it = out.setdefault(vid, {'video_id': vid, 'title': title, 'library': lib, 'paths': []})
        if path:
            it['paths'].append({'path': path, 'size': size})
    for it in out.values():
        if not it['title'] and it['paths']:
            it['title'] = os.path.splitext(os.path.basename(it['paths'][0]['path']))[0]
    return sorted(out.values(), key=lambda it: (it['title'] or '').lower())
