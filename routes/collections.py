# This file is: ./routes/collections.py
"""Collections (roadmap #9): /api/collections. Manual or smart; see collections_store.py."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

import collections_store as cs
from helpers import check_auth

router = APIRouter(prefix='/api/collections', tags=['collections'])
_model = None
_watch_history = None


def set_model(model):
    global _model
    _model = model


def set_watch_history(wh):
    global _watch_history
    _watch_history = wh


def get_model():
    return _model


def _conn():
    return _model.db.get_connection()


def _err(e):
    return {'success': False, 'error': str(e)}


@router.get('')
def list_collections(request: Request, library: Optional[str] = None, model=Depends(get_model)):
    try:
        return {'success': True, 'collections': cs.listing(_model, _conn(), library, request, check_auth(request))}
    except Exception as e:
        return _err(e)


class CreateBody(BaseModel):
    name: str
    kind: str = 'collection'
    rules: Optional[dict] = None
    description: str = ''
    video_ids: List[str] = []


@router.post('/create')
def create(body: CreateBody, model=Depends(get_model)):
    try:
        conn = _conn()
        cid = cs.create(conn, body.name, body.kind, body.rules, body.description, body.video_ids)
        return {'success': True, 'collection': cs.get(conn, cid)}
    except Exception as e:
        return _err(e)


class UpdateBody(BaseModel):
    id: int
    changes: dict = {}


@router.post('/update')
def update(body: UpdateBody, model=Depends(get_model)):
    try:
        return {'success': True, 'collection': cs.update(_conn(), body.id, body.changes or {})}
    except Exception as e:
        return _err(e)


class IdBody(BaseModel):
    id: int


@router.post('/delete')
def delete(body: IdBody, model=Depends(get_model)):
    try:
        cs.delete(_conn(), body.id)
        return {'success': True}
    except Exception as e:
        return _err(e)


class VideosBody(BaseModel):
    id: int
    video_ids: List[str] = []


@router.post('/add')
def add(body: VideosBody, model=Depends(get_model)):
    try:
        return {'success': True, 'added': cs.add(_conn(), body.id, body.video_ids)}
    except Exception as e:
        return _err(e)


@router.post('/remove')
def remove(body: VideosBody, model=Depends(get_model)):
    try:
        return {'success': True, 'removed': cs.remove(_conn(), body.id, body.video_ids)}
    except Exception as e:
        return _err(e)


@router.get('/{cid}/items')
def items(cid: int, request: Request, page: int = 1, limit: int = 100, seed: int = 0, model=Depends(get_model)):
    try:
        return {'success': True, **cs.items(_model, _conn(), cid, request, check_auth(request), _watch_history,
                                             max(1, page), max(1, min(limit, 500)), seed)}
    except Exception as e:
        return _err(e)
