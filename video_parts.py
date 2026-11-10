"""Multi-part videos: one video split over several files (cd1, cd2 ...), joined by hand.

A part is a file_versions row with `part` set (1, 2, ...). Part 1 is the video's preferred row, so
everything that shows one card per video keeps working; the player plays the parts as one timeline.
Parts and alternate versions don't mix: a video's live files are either all parts or none.
"""
import os
import re

_PART_TAIL = re.compile(r'[\s._\-\[(]*(?:cd|disc|disk|part|pt)[\s._\-]*\d{1,2}[\])]?\s*$', re.I)


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name or '')]


def order_paths(paths):
    """Paths in part order: by filename, numbers compared as numbers (cd2 before cd10)."""
    return sorted(paths, key=lambda p: natural_key(os.path.basename(p)))


def title_from(filename):
    """'Movie (2001) cd1.avi' -> 'Movie (2001)': the joined video's default title."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    return _PART_TAIL.sub('', stem).strip(' ._-') or stem


def has_column(conn):
    return 'part' in {r[1] for r in conn.execute('PRAGMA table_info(file_versions)').fetchall()}


def parts(conn, video_id):
    """-> the video's live parts in order: [{path, filename, part, length_seconds, cache_id, ...}], [] if it isn't multi-part."""
    if not video_id or not has_column(conn):
        return []
    cols = ('path', 'filename', 'part', 'length_seconds', 'cache_id', 'length', 'resolution', 'codec', 'file_size')
    rows = conn.execute(f'SELECT {", ".join(cols)} FROM file_versions '
                        'WHERE video_id = ? AND deleted_at IS NULL AND part IS NOT NULL ORDER BY part, path',
                        (video_id,)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def video_of(conn, path):
    r = conn.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,)).fetchone()
    return r[0] if r else None


def is_multipart(conn, video_id):
    return bool(parts(conn, video_id))


def set_order(conn, video_id, paths):
    """Number these files 1..n as the video's parts and make part 1 the preferred file."""
    live = {r[0] for r in conn.execute('SELECT path FROM file_versions WHERE video_id = ? AND deleted_at IS NULL',
                                       (video_id,)).fetchall()}
    if not paths or set(paths) - live:
        raise ValueError('Those files are not all part of this video')
    if len(set(paths)) != len(paths):
        raise ValueError('A file is listed twice')
    conn.executemany('UPDATE file_versions SET part = ? WHERE path = ?', [(i + 1, p) for i, p in enumerate(paths)])
    _lead(conn, video_id, paths[0])
    conn.commit()


def renumber(conn, video_id):
    """After a part left: 1..n again; a single part left is just a normal file."""
    ps = parts(conn, video_id)
    if len(ps) <= 1:
        conn.execute('UPDATE file_versions SET part = NULL WHERE video_id = ?', (video_id,))
    else:
        conn.executemany('UPDATE file_versions SET part = ? WHERE path = ?', [(i + 1, p['path']) for i, p in enumerate(ps)])
        _lead(conn, video_id, ps[0]['path'])
    conn.commit()


def _lead(conn, video_id, path):
    conn.execute('UPDATE file_versions SET is_preferred = (path = ?) WHERE video_id = ?', (path, video_id))
    conn.execute('UPDATE videos SET library = (SELECT library FROM file_versions WHERE path = ?) WHERE video_id = ? '
                 'AND (SELECT library FROM file_versions WHERE path = ?) IS NOT NULL', (path, video_id, path))


def fmt_length(seconds):
    s = int(seconds or 0)
    return f'{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}'


def annotate(item, versions):
    """Give a listing item its versions (2+ files) or, for a multi-part video, its parts and total length."""
    if not versions:
        return item
    ps = sorted((v for v in versions if v.get('part') is not None), key=lambda v: v['part'])
    if ps:
        item['parts'] = [{'path': v['path'], 'filename': v.get('filename') or os.path.basename(v['path']),
                          'part': v['part'], 'length': v.get('length') or '', 'length_seconds': v.get('length_seconds') or 0,
                          'resolution': v.get('resolution') or '', 'codec': v.get('codec') or '',
                          'file_size': v.get('file_size') or '', 'cache_id': v.get('cache_id')} for v in ps]
        total = sum(p['length_seconds'] for p in item['parts'])
        if total and all(p['length_seconds'] for p in item['parts']):
            item['length_seconds'] = total
            item['length'] = fmt_length(total)
        item.pop('versions', None)
    elif len(versions) > 1:
        item['versions'] = versions
    return item


def annotate_from_db(conn, item):
    """annotate() for a listing that builds its own versions: adds parts (and drops versions) for a multi-part video."""
    ps = parts(conn, item.get('video_id'))
    if ps:
        annotate(item, ps)
    return item


def length_of(conn, path):
    """A file's length in seconds: the stored one, else ffprobe's."""
    r = conn.execute('SELECT length_seconds FROM file_versions WHERE path = ?', (path,)).fetchone()
    if r and r[0]:
        return float(r[0])
    try:
        import transcoder
        return float(transcoder.probe(path)['duration'] or 0)
    except Exception:
        return 0.0


def shift_markers_for_join(conn, video_id, paths):
    """Before joining: markers made on a later part's own video move to that part's place on the joined timeline.
    (Only a video whose single file is that part; markers then follow the file into the joined video.)"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'video_markers'").fetchone():
        return
    offset = 0.0
    for path in paths:
        vid = video_of(conn, path)
        if offset and vid and vid != video_id:
            files = conn.execute('SELECT COUNT(*) FROM file_versions WHERE video_id = ? AND deleted_at IS NULL', (vid,)).fetchone()[0]
            if files == 1:
                conn.execute('UPDATE video_markers SET seconds = seconds + ?, end_seconds = end_seconds + ? WHERE video_id = ?',
                             (offset, offset, vid))
        offset += length_of(conn, path)
    conn.commit()


def offset_of(conn, path):
    """Where a file starts on its video's timeline: 0, or the length of the parts before it."""
    ps = parts(conn, video_of(conn, path))
    offset = 0.0
    for p in ps:
        if p['path'] == path:
            return offset
        offset += float(p['length_seconds'] or 0) or length_of(conn, p['path'])
    return 0.0
