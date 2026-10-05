# This file is: ./routes/player.py
"""
The new player (roadmap #8): stream list, HLS per resolution, settings.
Separate from routes/streaming.py (the classic player); see transcoder.py.

Settings live in the metadata table under 'player'.
"""
import json
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

import sprites
import transcoder
from media_handler import MediaHandler

router = APIRouter(prefix='/api/player', tags=['player'])
_model = None

SETTINGS_KEY = 'player'
DEFAULTS = {'hw': True, 'max_res': 'original', 'default_source': 'auto', 'sprites_on_open': True}
SOURCES = ('auto', 'direct') + tuple(r for r, _ in transcoder.RESOLUTIONS)
DIRECT_TYPES = {'mp4': 'video/mp4', 'm4v': 'video/mp4', 'mov': 'video/mp4', 'webm': 'video/webm',
                'mkv': 'video/webm', 'ogv': 'video/ogg'}


def set_model(model):
    global _model
    _model = model


def get_model():
    return _model


def _conn():
    return _model.db.get_connection()


def _err(e):
    return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def get_settings(conn):
    s = dict(DEFAULTS)
    try:
        row = conn.execute('SELECT value FROM metadata WHERE key = ?', (SETTINGS_KEY,)).fetchone()
        saved = json.loads(row[0]) if row else {}
    except Exception:
        saved = {}
    for k in ('hw', 'sprites_on_open'):
        if k in saved:
            s[k] = bool(saved[k])
    if saved.get('max_res') in dict(transcoder.RESOLUTIONS):
        s['max_res'] = saved['max_res']
    if saved.get('default_source') in SOURCES:
        s['default_source'] = saved['default_source']
    return s


def save_settings(conn, changes):
    s = get_settings(conn)
    for k in ('hw', 'sprites_on_open'):
        if k in changes:
            s[k] = bool(changes[k])
    if changes.get('max_res') in dict(transcoder.RESOLUTIONS):
        s['max_res'] = changes['max_res']
    if changes.get('default_source') in SOURCES:
        s['default_source'] = changes['default_source']
    conn.execute('INSERT INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
                 (SETTINGS_KEY, json.dumps(s)))
    conn.commit()
    return s


class SettingsBody(BaseModel):
    changes: dict = {}


@router.get('/settings')
def settings_get(model=Depends(get_model)):
    try:
        return {'success': True, 'settings': get_settings(_conn()), 'hw': transcoder.HW,
                'resolutions': [r for r, _ in transcoder.RESOLUTIONS], 'streams': transcoder.manager.status()}
    except Exception as e:
        return _err(e)


@router.post('/settings')
def settings_set(body: SettingsBody, model=Depends(get_model)):
    try:
        return {'success': True, 'settings': save_settings(_conn(), body.changes or {}), 'hw': transcoder.HW}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# streams
# ---------------------------------------------------------------------------
def _file_row(conn, path):
    return conn.execute('SELECT video_id, cache_id, codec, extension, length_seconds FROM file_versions '
                        'WHERE path = ? AND deleted_at IS NULL', (path,)).fetchone()


def hls_url(path, res, name='index.m3u8'):
    return f'/api/player/hls/{res}/{name}?path={quote(path, safe="")}'


def stream_list(conn, path, settings=None):
    """-> {video_id, duration, width, height, codec, cache_id, sources} for one file."""
    row = _file_row(conn, path)
    if not row:
        raise FileNotFoundError('Not in your library: ' + path)
    s = settings or get_settings(conn)
    info = transcoder.probe(path)
    duration = info['duration'] or float(row[4] or 0)
    ext = (row[3] or os.path.splitext(path)[1].lstrip('.')).lower()
    codec = info['codec'] or row[2] or ''
    likely = MediaHandler.is_browser_supported(ext, codec)
    sources = [{'kind': 'direct', 'label': 'Direct', 'res': 'direct', 'likely': bool(likely),
                'type': DIRECT_TYPES.get(ext, 'video/mp4'), 'src': '/files' + quote(path)}]
    for r in transcoder.resolutions(info['width'], info['height'], s['max_res']):
        sources.append({'kind': 'hls', 'label': 'HLS ' + transcoder.resolution_label(r, info['width'], info['height']),
                        'res': r, 'likely': True, 'type': 'application/x-mpegURL', 'src': hls_url(path, r)})
    cache_id = MediaHandler._get_cache_key(path, row[0], row[1])
    return {'video_id': row[0], 'path': path, 'duration': duration, 'width': info['width'], 'height': info['height'],
            'codec': codec, 'audio': info['audio'], 'cache_id': cache_id, 'sources': sources,
            'start_source': pick_source(sources, s['default_source']), 'sprite': sprite_info(cache_id)}


def sprite_info(cache_id):
    return {'ready': sprites.ready(cache_id), 'vtt': f'/cache/{cache_id}/{sprites.VTT}?v={sprites.version(cache_id)}'}


def pick_source(sources, default):
    """Index of the source to start with."""
    by_res = {x['res']: i for i, x in enumerate(sources)}
    if default == 'direct' or (default == 'auto' and sources[0]['likely']):
        return 0
    if default in by_res:
        return by_res[default]
    return 1 if len(sources) > 1 else 0          # auto with an unlikely direct: the best HLS


@router.get('/streams')
def streams(path: str = Query(...), model=Depends(get_model)):
    try:
        conn = _conn()
        s = get_settings(conn)
        d = stream_list(conn, path, s)
        if not d['sprite']['ready'] and s['sprites_on_open']:
            sprites.worker.enqueue(path, d['cache_id'], front=True)
            d['sprite']['queued'] = True
        return {'success': True, **d}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# seek-bar previews (sprites)
# ---------------------------------------------------------------------------
@router.get('/sprites/check')
def sprites_check(path: str = Query(...), model=Depends(get_model)):
    try:
        row = _file_row(_conn(), path)
        if not row:
            raise FileNotFoundError('Not in your library')
        return {'success': True, **sprite_info(MediaHandler._get_cache_key(path, row[0], row[1]))}
    except Exception as e:
        return _err(e)


def library_files(model, conn, library=None):
    """[(path, cache_id)] of the preferred files in a library, a group, or everything."""
    where, params = 'fv.is_preferred = 1 AND fv.deleted_at IS NULL', []
    if library and library not in ('All Videos', 'Home', 'all'):
        libs = (model.get_libraries_in_group(library) if library in (getattr(model, 'library_groups', None) or {})
                else [library])
        where += f' AND v.library IN ({",".join("?" * len(libs))})'
        params += list(libs)
    rows = conn.execute(f'SELECT fv.path, fv.video_id, fv.cache_id FROM file_versions fv JOIN videos v ON v.video_id = fv.video_id '
                        f'WHERE {where} ORDER BY fv.path', params).fetchall()
    return [(r[0], MediaHandler._get_cache_key(r[0], r[1], r[2])) for r in rows]


class SpriteBulkBody(BaseModel):
    library: str = 'All Videos'
    force: bool = False


@router.post('/sprites/generate')
def sprites_generate(body: SpriteBulkBody, model=Depends(get_model)):
    try:
        items = library_files(_model, _conn(), body.library)
        n = sprites.worker.start_bulk(items, force=body.force)
        return {'success': True, 'files': len(items), 'queued': n, 'status': sprites.worker.status()}
    except Exception as e:
        return _err(e)


@router.get('/sprites/status')
def sprites_status(model=Depends(get_model)):
    return {'success': True, 'status': sprites.worker.status()}


@router.post('/sprites/stop')
def sprites_stop(model=Depends(get_model)):
    return {'success': True, 'dropped': sprites.worker.stop_bulk(), 'status': sprites.worker.status()}


def _check(path, res):
    if res not in dict(transcoder.RESOLUTIONS):
        raise ValueError(f'Unknown resolution: {res}')
    if not _file_row(_conn(), path):
        raise FileNotFoundError('Not in your library')


@router.get('/hls/{res}/index.m3u8')
def hls_playlist(res: str, path: str = Query(...), model=Depends(get_model)):
    try:
        _check(path, res)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)
    info = transcoder.probe(path)
    text = transcoder.playlist(info['duration'], lambda n: hls_url(path, res, f'{n}.ts'))
    return PlainTextResponse(text, media_type='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-store'})


@router.get('/hls/{res}/{segment}.ts')
def hls_segment(res: str, segment: int, path: str = Query(...), model=Depends(get_model)):
    try:
        _check(path, res)
        hw = get_settings(_conn())['hw']
        f = transcoder.manager.segment(path, res, segment, hw=hw)
    except FileNotFoundError as e:
        return PlainTextResponse(str(e), status_code=404)
    except transcoder.SegmentError as e:
        return PlainTextResponse(str(e), status_code=500)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    return FileResponse(f, media_type='video/mp2t')


# ---------------------------------------------------------------------------
# markers: named points (or ranges) in a video, per video_id
# ---------------------------------------------------------------------------
_markers_ready = False


def ensure_markers(conn):
    global _markers_ready
    if _markers_ready:
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS video_markers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        seconds REAL NOT NULL,
        end_seconds REAL,                  -- NULL = a point, else a range
        title TEXT,
        tag TEXT,                          -- one of your 'tags' values (colours the marker)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_video_markers_video ON video_markers(video_id, seconds)')
    conn.commit()
    _markers_ready = True


def list_markers(conn, video_id):
    ensure_markers(conn)
    return [{'id': r[0], 'seconds': r[1], 'end_seconds': r[2], 'title': r[3] or '', 'tag': r[4] or ''}
            for r in conn.execute('SELECT id, seconds, end_seconds, title, tag FROM video_markers WHERE video_id = ? '
                                  'ORDER BY seconds, id', (video_id,))]


def tag_vocab(conn, limit=2000):
    """Your tags values, most used first (marker tag suggestions)."""
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(video_tags_junction)')]
        fk = next(c for c in cols if c.endswith('_id') and c != 'video_id')
        return [r[0] for r in conn.execute(f'SELECT t.value FROM video_tags t JOIN video_tags_junction j ON j.{fk} = t.id '
                                           f'GROUP BY t.value ORDER BY COUNT(*) DESC, t.value LIMIT ?', (limit,))]
    except Exception:
        return []


class MarkerBody(BaseModel):
    id: Optional[int] = None
    video_id: str
    seconds: float
    end_seconds: Optional[float] = None
    title: str = ''
    tag: str = ''


class MarkerDeleteBody(BaseModel):
    id: int


def save_marker(conn, b):
    ensure_markers(conn)
    title, tag = (b.title or '').strip(), (b.tag or '').strip()
    if not title and not tag:
        raise ValueError('Give the marker a title or a tag')
    if b.seconds is None or b.seconds < 0:
        raise ValueError('The marker needs a time')
    end = b.end_seconds
    if end is not None and end <= b.seconds:
        raise ValueError('The end must be after the start')
    if not conn.execute('SELECT 1 FROM videos WHERE video_id = ?', (b.video_id,)).fetchone():
        raise ValueError('Unknown video')
    if b.id:
        n = conn.execute('UPDATE video_markers SET seconds = ?, end_seconds = ?, title = ?, tag = ?, updated_at = CURRENT_TIMESTAMP '
                         'WHERE id = ? AND video_id = ?', (b.seconds, end, title, tag, b.id, b.video_id)).rowcount
        if not n:
            raise ValueError('That marker no longer exists')
        mid = b.id
    else:
        mid = conn.execute('INSERT INTO video_markers (video_id, seconds, end_seconds, title, tag) VALUES (?, ?, ?, ?, ?)',
                           (b.video_id, b.seconds, end, title, tag)).lastrowid
    conn.commit()
    return mid


@router.get('/markers')
def markers_get(video_id: str = Query(...), model=Depends(get_model)):
    try:
        conn = _conn()
        return {'success': True, 'markers': list_markers(conn, video_id), 'tags': tag_vocab(conn)}
    except Exception as e:
        return _err(e)


@router.post('/markers')
def markers_save(body: MarkerBody, model=Depends(get_model)):
    try:
        conn = _conn()
        mid = save_marker(conn, body)
        return {'success': True, 'id': mid, 'markers': list_markers(conn, body.video_id)}
    except Exception as e:
        return _err(e)


@router.post('/markers/delete')
def markers_delete(body: MarkerDeleteBody, model=Depends(get_model)):
    try:
        conn = _conn()
        ensure_markers(conn)
        row = conn.execute('SELECT video_id FROM video_markers WHERE id = ?', (body.id,)).fetchone()
        conn.execute('DELETE FROM video_markers WHERE id = ?', (body.id,))
        conn.commit()
        return {'success': True, 'markers': list_markers(conn, row[0]) if row else []}
    except Exception as e:
        return _err(e)


def start_background():
    """Called once at server start: clean the HLS folder, start the monitor, test VAAPI."""
    import threading
    transcoder.manager.start()
    threading.Thread(target=transcoder.detect_hw, name='player-hw-detect', daemon=True).start()
