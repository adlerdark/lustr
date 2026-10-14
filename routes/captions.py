# This file is: ./routes/captions.py
"""
Closed captions (captions.py): tracks for a video, making them (one video, a selection, a library),
the queue, settings. Settings live in the metadata table under 'captions'.
"""
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


def start_background():
    """At server start: the worker reads the settings; the night window feeds it libraries."""
    captions.worker.settings_fn = lambda: captions.get_settings(_conn())
    captions.start_night_scheduler(lambda: captions.get_settings(_conn()), files_without_captions)


def player_info(conn, path, cache_id):
    """For /api/player/streams: the tracks, whether it's being made, and (setting) queue it on play."""
    s = captions.get_settings(conn)
    tr = captions.tracks(cache_id)
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


@router.get('/status')
def status(path: Optional[str] = None, model=Depends(get_model)):
    out = {'success': True, 'status': captions.worker.status()}
    if path:
        try:
            out['tracks'] = captions.tracks(cache_id_of(_conn(), path))
        except Exception:
            out['tracks'] = []
    return out


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
        n = sum(captions.remove(cache_id_of(conn, p), body.file) for p in body.paths or [])
        return {'success': True, 'removed': n}
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
