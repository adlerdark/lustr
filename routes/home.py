# This file is: ./routes/home.py
"""Home / Recommended rows (roadmap #9): GET /api/home, row settings. See home.py."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import home
from helpers import check_auth

router = APIRouter(prefix='/api/home', tags=['home'])
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


def _collection_rows():
    try:
        import collections_store
        return collections_store.home_rows
    except Exception:
        return None


@router.get('')
def home_rows(request: Request, library: Optional[str] = None, model=Depends(get_model)):
    try:
        username = check_auth(request)
        return {'success': True, **home.build(_model, username, library, _watch_history, _collection_rows())}
    except Exception as e:
        return {'success': False, 'error': str(e)}


class RowSettingsBody(BaseModel):
    order: Optional[List[str]] = None
    hidden: Optional[List[str]] = None


@router.get('/settings')
def settings_get(model=Depends(get_model)):
    conn = _model.db.get_connection()
    return {'success': True, 'settings': home.get_settings(conn), 'titles': home.ROWS}


@router.post('/settings')
def settings_set(body: RowSettingsBody, model=Depends(get_model)):
    try:
        return {'success': True, 'settings': home.save_settings(_model.db.get_connection(), body.order, body.hidden)}
    except Exception as e:
        return {'success': False, 'error': str(e)}
