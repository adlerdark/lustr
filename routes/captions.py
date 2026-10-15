# This file is: ./routes/captions.py
"""
Closed captions (captions.py): tracks for a video, making them (one video, a selection, a library),
the queue, settings. Settings live in the metadata table under 'captions'.
"""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import captions
from media_handler import MediaHandler

router = APIRouter(prefix='/api/captions', tags=['captions'])
_model = None


def set_model(model):
    global _model
    _model = model


def get_model():
    return _model


def _conn():
    return _model.db.get_connection()


def _err(e):
    return {'success': False, 'error': str(e)}


def cache_id_of(conn, path):
    row = conn.execute('SELECT video_id, cache_id FROM file_versions WHERE path = ? AND deleted_at IS NULL', (path,)).fetchone()
    if not row:
        raise FileNotFoundError('Not in your library: ' + path)
    return MediaHandler._get_cache_key(path, row[0], row[1])


def files_without_captions(libraries):
    """[(path, cache_id)] of the preferred files in these libraries (or groups)."""
    import routes.player
    out = []
    for lib in libraries:
        out += routes.player.library_files(_model, _conn(), lib)
    return out


# ---------------------------------------------------------------------------
# which videos have captions (video_captions): for the CC badge and captions:yes / captions:no
# ---------------------------------------------------------------------------
def ensure_table(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS video_captions (video_id TEXT PRIMARY KEY, langs TEXT, sources TEXT, '
                 'updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')


def sync_one(path, cache_id=None, conn=None):
    """Record (or forget) one video's captions."""
    conn = conn or _conn()
    ensure_table(conn)
    row = conn.execute('SELECT video_id, cache_id FROM file_versions WHERE path = ?', (path,)).fetchone()
    if not row:
        return
    cid = cache_id or MediaHandler._get_cache_key(path, row[0], row[1])
    tr = captions.tracks(cid)
    if tr:
        conn.execute('INSERT INTO video_captions (video_id, langs, sources, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) '
                     'ON CONFLICT(video_id) DO UPDATE SET langs = excluded.langs, sources = excluded.sources, '
                     'updated_at = CURRENT_TIMESTAMP',
                     (row[0], ','.join(sorted({t['lang'] for t in tr})), ','.join(sorted({t['source'] or '' for t in tr}))))
    else:
        conn.execute('DELETE FROM video_captions WHERE video_id = ?', (row[0],))
    conn.commit()


def sync_all(conn=None):
    """Rebuild the list from the caption files in the cache. -> videos with captions"""
    conn = conn or _conn()
    ensure_table(conn)
    found = {}
    for path, vid, cid in conn.execute('SELECT path, video_id, cache_id FROM file_versions WHERE deleted_at IS NULL').fetchall():
        key = MediaHandler._get_cache_key(path, vid, cid)
        if vid not in found and os.path.exists(os.path.join(captions.folder(key), captions.INDEX)):
            tr = captions.tracks(key)
            if tr:
                at = datetime.utcfromtimestamp(os.path.getmtime(os.path.join(captions.folder(key), captions.INDEX)))
                found[vid] = (','.join(sorted({t['lang'] for t in tr})), ','.join(sorted({t['source'] or '' for t in tr})),
                              at.strftime('%Y-%m-%d %H:%M:%S'))
    # dated by the caption files (the Done list in Settings is ordered by it), not by this rebuild
    conn.execute('DELETE FROM video_captions')
    conn.executemany('INSERT INTO video_captions (video_id, langs, sources, updated_at) VALUES (?, ?, ?, ?)',
                     [(v, l, s, at) for v, (l, s, at) in found.items()])
    conn.commit()
    return len(found)


def start_background():
    """At server start: the worker reads the settings and keeps the list current; the night window
    feeds it libraries; the 'has captions' list is rebuilt once in the background."""
    import threading
    captions.worker.settings_fn = lambda: captions.get_settings(_conn())
    captions.worker.on_change = lambda path, cid: sync_one(path, cid)
    captions.start_night_scheduler(lambda: captions.get_settings(_conn()), files_without_captions)

    def fill():
        try:
            print(f'[captions] {sync_all()} videos have captions')
        except Exception as e:
            print(f'[captions] could not list captioned videos: {e}')
    threading.Thread(target=fill, name='captions-index', daemon=True).start()


def _other_version_tracks(conn, path):
    """Captions of another file of the same video (merged versions), closest in length first. -> tracks or []"""
    row = conn.execute('SELECT video_id, length_seconds FROM file_versions WHERE path = ?', (path,)).fetchone()
    if not row:
        return []
    others = conn.execute('SELECT path, cache_id, length_seconds FROM file_versions WHERE video_id = ? AND path != ? '
                          'AND deleted_at IS NULL', (row[0], path)).fetchall()
    others.sort(key=lambda o: abs((o[2] or 0) - (row[1] or 0)) if row[1] and o[2] else float('inf'))
    for p, cid, _ in others:
        tr = captions.tracks(MediaHandler._get_cache_key(p, row[0], cid))
        if tr:
            return [{**t, 'other_file': os.path.basename(p), 'label': t['label'] + ' - other version'} for t in tr]
    return []


def player_info(conn, path, cache_id):
    """For /api/player/streams: the tracks, whether it's being made, and (setting) queue it on play.
    A file without captions borrows them from another version of the same video."""
    s = captions.get_settings(conn)
    tr = captions.tracks(cache_id) or _other_version_tracks(conn, path)
    queued = cache_id in captions.worker.queued_ids() or captions.worker.status().get('current_cache_id') == cache_id
    if not tr and s['auto_on_play'] and captions.whisper_available():
        queued = captions.worker.enqueue(path, cache_id, front=True) or queued
    return {'tracks': tr, 'queued': bool(queued), 'whisper': captions.whisper_available()}


@router.get('')
def list_tracks(path: str = Query(...), model=Depends(get_model)):
    try:
        cid = cache_id_of(_conn(), path)
        return {'success': True, 'cache_id': cid, 'tracks': captions.tracks(cid),
                'sidecars': [s[1] for s in captions.sidecars(path)]}
    except Exception as e:
        return _err(e)


class GenerateBody(BaseModel):
    paths: list = []
    library: Optional[str] = None
    front: bool = False
    force: bool = False


@router.post('/generate')
def generate(body: GenerateBody, model=Depends(get_model)):
    try:
        conn = _conn()
        if body.library:
            items = files_without_captions([body.library])
        else:
            items = [(p, cache_id_of(conn, p)) for p in body.paths or []]
        if len(items) == 1 and not body.library:
            p, cid = items[0]
            if body.force or not captions.has_captions(cid):
                captions.worker.enqueue(p, cid, front=True if body.front or len(items) == 1 else False, force=body.force)
                n = 1
            else:
                n = 0
        else:
            n = captions.worker.start_bulk(items, force=body.force)
        return {'success': True, 'files': len(items), 'queued': n, 'whisper': captions.whisper_available(),
                'status': captions.worker.status()}
    except Exception as e:
        return _err(e)


def _done(conn, limit=100):
    """Videos with captions, newest first -> ([{video_id, title, path, langs, sources, at}], total)"""
    ensure_table(conn)
    total = conn.execute('SELECT COUNT(*) FROM video_captions').fetchone()[0]
    rows = conn.execute('''SELECT c.video_id, v.title, (SELECT fv.path FROM file_versions fv WHERE fv.video_id = c.video_id
                                  AND fv.deleted_at IS NULL ORDER BY fv.is_preferred DESC LIMIT 1), c.langs, c.sources, c.updated_at
                           FROM video_captions c LEFT JOIN videos v ON v.video_id = c.video_id
                           ORDER BY c.updated_at DESC LIMIT ?''', (limit,)).fetchall()
    return [{'video_id': r[0], 'title': r[1] or '', 'path': r[2], 'file': os.path.basename(r[2] or ''), 'langs': r[3] or '',
             'sources': r[4] or '', 'at': r[5]} for r in rows], total


@router.get('/status')
def status(path: Optional[str] = None, details: bool = False, model=Depends(get_model)):
    out = {'success': True, 'status': captions.worker.status()}
    if details:
        out['queue'] = captions.worker.queue_list()
        out['history'] = list(reversed(captions.worker.history))
        try:
            out['done'], out['done_total'] = _done(_conn())
        except Exception as e:
            out['done'], out['done_total'] = [], 0
            print(f'[captions] done list: {e}')
    if path:
        try:
            out['tracks'] = captions.tracks(cache_id_of(_conn(), path))
        except Exception:
            out['tracks'] = []
    return out


class DropBody(BaseModel):
    cache_id: str


@router.post('/drop')
def drop(body: DropBody, model=Depends(get_model)):
    return {'success': True, 'dropped': captions.worker.drop(body.cache_id), 'status': captions.worker.status()}


@router.post('/stop')
def stop(model=Depends(get_model)):
    return {'success': True, 'dropped': captions.worker.stop(), 'status': captions.worker.status()}


class RemoveBody(BaseModel):
    paths: list = []
    file: Optional[str] = None       # one track; None = all of a video's captions


@router.post('/remove')
def remove(body: RemoveBody, model=Depends(get_model)):
    try:
        conn = _conn()
        n = 0
        for p in body.paths or []:
            n += captions.remove(cache_id_of(conn, p), body.file)
            sync_one(p, conn=conn)
        return {'success': True, 'removed': n}
    except Exception as e:
        return _err(e)


@router.get('/index')
def index(model=Depends(get_model)):
    """video_ids that have captions (the CC badge)."""
    try:
        conn = _conn()
        ensure_table(conn)
        return {'success': True, 'video_ids': [r[0] for r in conn.execute('SELECT video_id FROM video_captions')]}
    except Exception as e:
        return _err(e)


@router.post('/index/rebuild')
def index_rebuild(model=Depends(get_model)):
    try:
        return {'success': True, 'videos': sync_all()}
    except Exception as e:
        return _err(e)


class SettingsBody(BaseModel):
    changes: dict = {}


@router.get('/settings')
def settings_get(model=Depends(get_model)):
    try:
        return {'success': True, 'settings': captions.get_settings(_conn()), 'models': captions.models_status(),
                'whisper': captions.whisper_available(), 'status': captions.worker.status()}
    except Exception as e:
        return _err(e)


@router.post('/settings')
def settings_set(body: SettingsBody, model=Depends(get_model)):
    try:
        return {'success': True, 'settings': captions.save_settings(_conn(), body.changes or {}),
                'models': captions.models_status()}
    except Exception as e:
        return _err(e)
