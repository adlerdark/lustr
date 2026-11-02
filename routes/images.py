# This file is: ./routes/images.py
"""Images tab: /api/images (albums by folder, an album's images and details, thumbnails, the files). See images.py."""
import os
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import images
from config import CACHE_DIR
from helpers import check_auth

router = APIRouter(prefix='/api/images', tags=['images'])
_model = None
_watch_history = None
KEEP = {'Cache-Control': 'private, max-age=31536000, immutable'}     # the URLs carry ?v=<file time>
FILL_NOW = 400                     # photo sizes read before an album opens (~1-2 s); the rest in the background


def set_model(model):
    global _model
    _model = model


def set_watch_history(wh):
    global _watch_history
    _watch_history = wh


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


def _filters(tags, tags_exclude, sites, sites_exclude, cast, favorites):
    return {'tags': tags, 'tags_exclude': tags_exclude, 'sites': sites, 'sites_exclude': sites_exclude,
            'cast': cast, 'favorites': favorites}


@router.get('/albums')
def albums(library: Optional[str] = None, sort: str = 'name', q: str = '', page: int = 1, limit: int = 120,
           tags: str = '', tags_exclude: str = '', sites: str = '', sites_exclude: str = '', cast: str = '',
           favorites: bool = False, model=Depends(get_model)):
    """One page of albums -> {albums, total, letters}. cast=<name> (with no library): a performer's albums."""
    try:
        conn = _conn()
        return {'success': True, **images.listing(conn, _shown_libraries(conn, library), sort, q, page, limit,
                                                  **_filters(tags, tags_exclude, sites, sites_exclude, cast, favorites))}
    except Exception as e:
        return _err(e)


@router.get('/filters')
def filters(library: Optional[str] = None, q: str = '', tags: str = '', tags_exclude: str = '', sites: str = '',
            sites_exclude: str = '', favorites: bool = False, model=Depends(get_model)):
    """The sidebar's Tags / Sites with album counts (Images tab)."""
    try:
        conn = _conn()
        return {'success': True, **images.filter_counts(conn, _shown_libraries(conn, library), q,
                                                        **_filters(tags, tags_exclude, sites, sites_exclude, '', favorites))}
    except Exception as e:
        return _err(e)


def _cards(conn, rows, request):
    import home
    username = check_auth(request)
    out = []
    for vid, path in rows:
        c = home.card(_model, path, username, _watch_history)
        if c:
            out.append(c)
    return out


@router.get('/album')
def album(folder: str, request: Request, model=Depends(get_model)):
    try:
        conn = _conn()
        if not _may_see(conn, folder=folder):
            return _err('Not found')
        if images.fill_sizes(conn, folder, limit=FILL_NOW) == FILL_NOW:      # a big album: the rest in the background
            images.fill_sizes_later(_model.db.get_connection, folder)
        meta = conn.execute('SELECT favorite FROM image_albums WHERE folder = ?', (folder,)).fetchone()
        return {'success': True, 'folder': folder, 'name': images.album_title(conn, folder), 'favorite': bool(meta and meta[0]),
                'meta': images.album_meta(conn, [folder]).get(folder, {}), 'images': images.album_images(conn, folder),
                'videos': _cards(conn, images.linked_videos(conn, folder), request)}
    except Exception as e:
        return _err(e)


@router.get('/album/sizes')
def album_sizes(folder: str, model=Depends(get_model)):
    """{id: [w, h]} known so far (the photos further down a big album, read in the background)."""
    try:
        conn = _conn()
        return {'success': True, 'sizes': images.sizes(conn, folder) if _may_see(conn, folder=folder) else {}}
    except Exception as e:
        return _err(e)


class FoldersBody(BaseModel):
    folders: List[str] = []


class MetaBody(FoldersBody):
    updates: Dict[str, str] = {}
    add: Dict[str, List[str]] = {}
    remove: Dict[str, List[str]] = {}


def _visible(conn, folders):
    ok = [f for f in folders if _may_see(conn, folder=f)]
    if not ok:
        raise ValueError('Not found')
    return ok


@router.post('/meta/info')
def meta_info(body: FoldersBody, request: Request, model=Depends(get_model)):
    """Edit Album: each album's title and values (several albums: the editor works out what they share);
    for one album, its linked videos too."""
    try:
        conn = _conn()
        folders = _visible(conn, body.folders)
        meta = images.album_meta(conn, folders)
        out = [{'folder': f, 'title': images.album_title(conn, f), 'meta': meta.get(f, {})} for f in folders]
        if len(out) == 1:
            out[0]['videos'] = _cards(conn, images.linked_videos(conn, folders[0]), request)
        return {'success': True, 'albums': out}
    except Exception as e:
        return _err(e)


@router.post('/meta')
def meta_save(body: MetaBody, model=Depends(get_model)):
    try:
        conn = _conn()
        images.set_album_meta(conn, _visible(conn, body.folders), body.updates, body.add, body.remove)
        return {'success': True}
    except Exception as e:
        return _err(e)


class LinkBody(BaseModel):
    folder: str
    video_ids: List[str] = []
    on: bool = True


@router.post('/videos/link')
def videos_link(body: LinkBody, request: Request, model=Depends(get_model)):
    """Link (on) or unlink videos -> the album's linked videos as cards."""
    try:
        conn = _conn()
        _visible(conn, [body.folder])
        images.link_videos(conn, body.folder, body.video_ids, body.on)
        return {'success': True, 'videos': _cards(conn, images.linked_videos(conn, body.folder), request)}
    except Exception as e:
        return _err(e)


@router.get('/videos/suggest')
def videos_suggest(folder: str, request: Request, model=Depends(get_model)):
    """Videos next to the album, as cards, the likeliest first."""
    try:
        conn = _conn()
        _visible(conn, [folder])
        return {'success': True, 'videos': _cards(conn, images.suggest_videos(conn, folder), request)}
    except Exception as e:
        return _err(e)


@router.get('/videos/search')
def videos_search(folder: str, q: str, request: Request, model=Depends(get_model)):
    """Find videos to link: the ones in the album's folder (or below it) first, then next to it, then the rest."""
    try:
        conn = _conn()
        _visible(conn, [folder])
        return {'success': True, 'videos': _cards(conn, images.search_videos(conn, folder, q), request)}
    except Exception as e:
        return _err(e)


@router.get('/by-video')
def by_video(path: str, model=Depends(get_model)):
    """The albums linked to a video (its File Info)."""
    try:
        conn = _conn()
        r = conn.execute('SELECT video_id FROM file_versions WHERE path = ?', (path,)).fetchone()
        found = images.albums_of_video(conn, r[0]) if r else []
        return {'success': True, 'albums': [a for a in found if _may_see(conn, folder=a['folder'])]}
    except Exception as e:
        return _err(e)


@router.get('/album/info')
def album_info(folder: str, model=Depends(get_model)):
    try:
        conn = _conn()
        info = images.album_info(conn, folder) if _may_see(conn, folder=folder) else None
        return {'success': True, 'info': info} if info else _err('Not found')
    except Exception as e:
        return _err(e)


@router.get('/{image_id}/info')
def image_info(image_id: int, model=Depends(get_model)):
    try:
        conn = _conn()
        info = images.image_info(conn, image_id) if _may_see(conn, image_id=image_id) else None
        return {'success': True, 'info': info} if info else _err('Not found')
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
    if not g or not _may_see(conn, image_id=image_id):
        return Response(status_code=404)
    kind = images.TYPES.get(os.path.splitext(g[0])[1].lower(), 'application/octet-stream')
    data = images.archived(conn, image_id)
    if data is not None:
        return Response(data, media_type=kind, headers=KEEP)
    if not os.path.isfile(g[0]):
        return Response(status_code=404)
    return FileResponse(g[0], media_type=kind, headers=KEEP)


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
