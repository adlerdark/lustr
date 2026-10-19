# This file is: ./routes/movies.py
"""Full movies: /api/movies (the Movies tab, a movie's page, Find Movies, ThePornDB links). See movies.py."""
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import movies
from helpers import check_auth

router = APIRouter(prefix='/api/movies', tags=['movies'])
_model = None
_watch_history = None
MAX_COVER = 10 * 1024 * 1024


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
    movies.ensure_schema(conn)
    return conn


def _err(e):
    return {'success': False, 'error': str(e)}


def _changed():
    import home
    home.invalidate()


@router.get('')
def list_movies(library: Optional[str] = None, q: str = '', sort: str = 'title', page: int = 1, limit: int = 120,
                model=Depends(get_model)):
    try:
        return {'success': True, **movies.listing(_model, _conn(), library, q, sort, max(1, page), max(1, min(limit, 500)))}
    except Exception as e:
        return _err(e)


@router.get('/autocomplete')
def autocomplete(q: str = '', model=Depends(get_model)):
    try:
        return {'success': True, 'titles': movies.autocomplete(_conn(), q)}
    except Exception as e:
        return _err(e)


@router.get('/find')
def find(library: Optional[str] = None, whole: bool = False, model=Depends(get_model)):
    """Find Movies: proposed movies (read-only)."""
    try:
        return {'success': True, 'groups': movies.find(_model, _conn(), library, whole)}
    except Exception as e:
        return _err(e)


class ApplyBody(BaseModel):
    groups: List[dict] = []


@router.post('/find/apply')
def find_apply(body: ApplyBody, model=Depends(get_model)):
    try:
        n, made = movies.apply_found(_conn(), body.groups)
        _changed()
        return {'success': True, 'videos': n, 'made': made}
    except Exception as e:
        return _err(e)


class MergeBody(BaseModel):
    ids: List[int]
    into: int


@router.post('/merge')
def merge(body: MergeBody, model=Depends(get_model)):
    try:
        n = movies.merge_into(_conn(), body.ids, body.into)
        return {'success': True, 'moved': n, 'movie': movies.get(_conn(), body.into)}
    except Exception as e:
        return _err(e)


@router.get('/{mid}')
def detail(mid: int, request: Request, model=Depends(get_model)):
    try:
        return {'success': True, **movies.detail(_model, _conn(), mid, check_auth(request), _watch_history)}
    except Exception as e:
        return _err(e)


class UpdateBody(BaseModel):
    changes: dict = {}
    merge: bool = False


@router.post('/{mid}/update')
def update(mid: int, body: UpdateBody, model=Depends(get_model)):
    try:
        out = movies.update(_conn(), mid, body.changes or {}, body.merge)
        return {'success': True, **out}
    except Exception as e:
        return _err(e)


@router.post('/{mid}/delete')
def delete(mid: int, model=Depends(get_model)):
    try:
        movies.delete(_conn(), mid)
        _changed()
        return {'success': True}
    except Exception as e:
        return _err(e)


@router.get('/{mid}/cover')
def get_cover(mid: int, model=Depends(get_model)):
    c = movies.cover(_conn(), mid)
    if not c:
        return Response(status_code=404)
    return Response(content=c[0], media_type=c[1], headers={'Cache-Control': 'private, max-age=86400'})


@router.post('/{mid}/cover')
async def upload_cover(mid: int, file: UploadFile = File(...), model=Depends(get_model)):
    try:
        if not (file.content_type or '').startswith('image/'):
            return _err('The cover must be an image')
        data = await file.read()
        if len(data) > MAX_COVER:
            return _err('The image must be under 10 MB')
        movies.set_cover(_conn(), mid, data, file.content_type)
        return {'success': True, 'movie': movies.get(_conn(), mid)}
    except Exception as e:
        return _err(e)


class CoverVideoBody(BaseModel):
    video_id: Optional[str] = None


@router.post('/{mid}/cover/video')
def cover_from_video(mid: int, body: CoverVideoBody, model=Depends(get_model)):
    """Use one of the movie's videos' thumbnail as the cover (none: back to the default)."""
    try:
        conn = _conn()
        conn.execute('UPDATE movies SET cover = NULL, cover_mime = NULL, cover_source = NULL, cover_video_id = ?, '
                     'updated_at = CURRENT_TIMESTAMP WHERE id = ?', (body.video_id or None, mid))
        conn.commit()
        return {'success': True, 'movie': movies.get(conn, mid)}
    except Exception as e:
        return _err(e)


class SearchBody(BaseModel):
    query: str = ''


@router.post('/{mid}/ext/search')
def ext_search(mid: int, body: SearchBody, model=Depends(get_model)):
    try:
        q = (body.query or '').strip() or movies.get(_conn(), mid)['title']
        return {'success': True, 'query': q, 'candidates': movies.tpdb_candidates(q)}
    except Exception as e:
        return _err(e)


class LinkBody(BaseModel):
    ext_id: str


@router.post('/{mid}/ext/link')
def ext_link(mid: int, body: LinkBody, model=Depends(get_model)):
    try:
        return {'success': True, 'movie': movies.link_tpdb(_conn(), mid, body.ext_id)}
    except Exception as e:
        return _err(e)


@router.post('/{mid}/ext/refresh')
def ext_refresh(mid: int, model=Depends(get_model)):
    try:
        m = movies.get(_conn(), mid)
        if not m['ext_id']:
            return _err('This movie is not linked to ThePornDB')
        return {'success': True, 'movie': movies.link_tpdb(_conn(), mid, m['ext_id'])}
    except Exception as e:
        return _err(e)


@router.post('/{mid}/ext/unlink')
def ext_unlink(mid: int, model=Depends(get_model)):
    try:
        return {'success': True, 'movie': movies.unlink_tpdb(_conn(), mid)}
    except Exception as e:
        return _err(e)
