# This file is: ./routes/images.py
"""Images tab: /api/images (albums by folder, an album's images, thumbnails, the files). See images.py."""
import os
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import images
from config import CACHE_DIR

router = APIRouter(prefix='/api/images', tags=['images'])
_model = None
KEEP = {'Cache-Control': 'private, max-age=31536000, immutable'}     # the URLs carry ?v=<file time>


def set_model(model):
    global _model
    _model = model


def get_model():
    return _model


def _conn():
    conn = _model.db.get_connection()
    images.ensure_schema(conn)
    return conn


def _err(e):
    return {'success': False, 'error': str(e)}


def _shown_libraries(conn, library=None):
    """The libraries this viewer may see (the display filter), narrowed to a library / group."""
    import display_filter
    libs = display_filter.visible_libraries(_model, display_filter.get_selection(conn))
    if library and library not in ('All Videos', 'Home'):
        groups = getattr(_model, 'library_groups', None) or {}
        wanted = set(_model.get_libraries_in_group(library) if library in groups else [library])
        libs = [l for l in libs if l in wanted]
    return libs


def _may_see(conn, image_id=None, folder=None):
    lib = images.library_of(conn, image_id=image_id, folder=folder)
    return lib is not None and lib in _shown_libraries(conn)


@router.get('/counts')
def counts(model=Depends(get_model)):
    try:
        conn = _conn()
        c = images.counts(conn, _shown_libraries(conn))
        for g in (getattr(_model, 'library_groups', None) or {}):              # a group: its libraries together
            n = sum(c.get(l, 0) for l in _model.get_libraries_in_group(g))
            if n and g not in (_model.libraries or {}):
                c[g] = n
        c['All Videos'] = sum(v for k, v in c.items() if k in (_model.libraries or {}))
        return {'success': True, 'counts': c}
    except Exception as e:
        return _err(e)


@router.get('/albums')
def albums(library: Optional[str] = None, sort: str = 'name', q: str = '', model=Depends(get_model)):
    try:
        conn = _conn()
        return {'success': True, 'albums': images.albums(conn, _shown_libraries(conn, library), sort, q)}
    except Exception as e:
        return _err(e)


@router.get('/album')
def album(folder: str, model=Depends(get_model)):
    try:
        conn = _conn()
        if not _may_see(conn, folder=folder):
            return _err('Not found')
        meta = conn.execute('SELECT favorite FROM image_albums WHERE folder = ?', (folder,)).fetchone()
        return {'success': True, 'folder': folder, 'name': os.path.basename(folder), 'favorite': bool(meta and meta[0]),
                'images': images.album_images(conn, folder)}
    except Exception as e:
        return _err(e)


@router.get('/{image_id}/thumb')
def thumb(image_id: int, model=Depends(get_model)):
    conn = _conn()
    if not _may_see(conn, image_id=image_id):
        return Response(status_code=404)
    p = images.thumb(conn, image_id, CACHE_DIR)
    if not p:
        return Response(status_code=404)
    return FileResponse(p, media_type='image/webp', headers=KEEP)


@router.get('/{image_id}/file')
def file(image_id: int, model=Depends(get_model)):
    """The original image. Found by id: only a path the scan recorded is ever served."""
    conn = _conn()
    g = images.get(conn, image_id)
    if not g or not _may_see(conn, image_id=image_id) or not os.path.isfile(g[0]):
        return Response(status_code=404)
    ext = os.path.splitext(g[0])[1].lower()
    return FileResponse(g[0], media_type=images.TYPES.get(ext, 'application/octet-stream'), headers=KEEP)


class FavoriteBody(BaseModel):
    ids: Optional[List[int]] = None
    folder: Optional[str] = None
    on: bool = True


@router.post('/favorite')
def favorite(body: FavoriteBody, model=Depends(get_model)):
    try:
        images.set_favorite(_conn(), ids=body.ids or [], folder=body.folder, on=body.on)
        return {'success': True}
    except Exception as e:
        return _err(e)


class CoverBody(BaseModel):
    folder: str
    id: int


@router.post('/cover')
def cover(body: CoverBody, model=Depends(get_model)):
    try:
        images.set_cover(_conn(), body.folder, body.id)
        return {'success': True}
    except Exception as e:
        return _err(e)


class SettingsBody(BaseModel):
    skip_folders: List[str] = []


@router.get('/settings')
def settings_get(model=Depends(get_model)):
    return {'success': True, 'skip_folders': images.skip_folders(_conn())}


@router.post('/settings')
def settings_set(body: SettingsBody, model=Depends(get_model)):
    try:
        return {'success': True, 'skip_folders': images.set_skip_folders(_conn(), body.skip_folders)}
    except Exception as e:
        return _err(e)
