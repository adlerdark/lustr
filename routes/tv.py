# This file is: ./routes/tv.py

"""
TV apps: link a TV with a 4-digit code (tv_pairing.py), and the TVs that are logged in.

/api/tv/pair/start and /api/tv/pair/poll are public (the TV isn't logged in yet); the rest
needs the owner's login, like every other /api route.
"""

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import auth_sessions
import tv_pairing
from config import AUTH_COOKIE_NAME
from helpers import LoginLimiter, check_auth, request_is_https

router = APIRouter(prefix="/api/tv", tags=["tv"])

start_limiter = LoginLimiter(limit=10, window=300)      # new codes per address
code_limiter = LoginLimiter(limit=5, window=300)        # wrong codes per user
MAX_BODY = 1024


class ApproveBody(BaseModel):
    code: str


class LogoutBody(BaseModel):
    id: Optional[str] = None
    all: bool = False


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else '?'


def _owner(request: Request) -> str:
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized or Session Expired")
    return username


def _mins(wait: int) -> str:
    mins = (wait + 59) // 60
    return f"{mins} minute{'' if mins == 1 else 's'}"


async def _small_json(request: Request) -> dict:
    """The body as a dict; empty for a missing, malformed or too big one."""
    body = await request.body()
    if not body or len(body) > MAX_BODY:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def start_tv_session(request: Request, response: Response, username: str, device: Optional[str] = None):
    """A TV login: a session of at most TV_SESSION_MAX_AGE, and a cookie that ends with it."""
    token = auth_sessions.create(username, kind='tv', max_age=tv_pairing.TV_SESSION_MAX_AGE, device=device)
    response.set_cookie(key=AUTH_COOKIE_NAME, value=token, max_age=tv_pairing.TV_SESSION_MAX_AGE,
                        httponly=True, samesite='lax', secure=request_is_https(request))


@router.post("/pair/start")
async def pair_start(request: Request):
    """A TV asks for a code to show."""
    ip = _client_ip(request)
    wait = start_limiter.retry_after(ip)
    if wait:
        raise HTTPException(status_code=429, headers={'Retry-After': str(wait)},
                            detail=f"Too many codes asked for. Try again in {_mins(wait)}.")
    start_limiter.failed(ip)                              # every start counts toward the limit
    data = await _small_json(request)
    name = data.get('device_name')
    return tv_pairing.start(name if isinstance(name, str) else None, ip)


@router.post("/pair/poll")
async def pair_poll(request: Request, response: Response):
    """The TV asks whether its code was entered; once it was, it gets the login cookie."""
    data = await _small_json(request)
    status, who = tv_pairing.claim(data.get('pair_id'))
    if status == 'approved':
        username, device = who
        start_tv_session(request, response, username, device)
    return {"status": status}


@router.post("/pair/approve")
def pair_approve(request: Request, body: ApproveBody):
    """The owner typed the code a TV is showing (Settings -> Account & Security -> Link a TV)."""
    username = _owner(request)
    wait = code_limiter.retry_after(username)
    if wait:
        raise HTTPException(status_code=429, headers={'Retry-After': str(wait)},
                            detail=f"Too many wrong codes. Try again in {_mins(wait)}.")
    r = tv_pairing.approve(body.code, username)
    if not r:
        code_limiter.failed(username)
        raise HTTPException(status_code=404, detail="No TV is showing that code.")
    code_limiter.succeeded(username)
    return {"success": True, "device_name": r['device_name']}


@router.get("/sessions")
def tv_sessions(request: Request):
    """The TVs that are logged in, newest first (times in epoch seconds)."""
    username = _owner(request)
    return {"sessions": [{k: s[k] for k in ('id', 'device', 'created', 'last_active', 'expires')}
                         for s in auth_sessions.list_sessions(username, kind='tv')]}


@router.post("/sessions/logout")
def tv_sessions_logout(request: Request, body: LogoutBody):
    """Log out one TV ({id}) or all of them ({all: true})."""
    username = _owner(request)
    if body.all:
        ids = [s['id'] for s in auth_sessions.list_sessions(username, kind='tv')]
    else:
        ids = [body.id] if body.id and any(s['id'] == body.id for s in auth_sessions.list_sessions(username, kind='tv')) else []
    ended = sum(auth_sessions.end_by_id(username, i) for i in ids)
    return {"success": True, "ended": ended}
