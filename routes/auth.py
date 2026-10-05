# This file is: ./routes/auth.py

"""
Authentication routes.
Handles user signup, login, logout, password change, and session management.
"""

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel
from helpers import sign_token, check_auth, session_token, request_is_https, login_limiter
from config import AUTH_COOKIE_NAME
import auth_sessions

router = APIRouter(prefix="/api", tags=["auth"])

# Global reference to user_model (will be set by web_server.py)
_user_model = None

MIN_PASSWORD = 8


def get_user_model():
    """Dependency function to get user_model."""
    return _user_model


class AuthRequest(BaseModel):
    """Authentication request model."""
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else '?'


def _start_session(request: Request, response: Response, username: str):
    """Log in: a new session and its cookie. No max_age, so closing the browser logs out too."""
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=sign_token(username),
        httponly=True,
        samesite='lax',
        secure=request_is_https(request),
    )


def _too_many(request: Request):
    """429 while this address is locked out for wrong passwords."""
    wait = login_limiter.retry_after(_client_ip(request))
    if wait:
        mins = (wait + 59) // 60
        raise HTTPException(status_code=429, headers={'Retry-After': str(wait)},
                            detail=f"Too many wrong passwords. Try again in {mins} minute{'' if mins == 1 else 's'}.")


@router.get("/max_users")
def get_max_user_info(user_model = Depends(get_user_model)):
    """Get user count and limits."""
    return {
        "max_users": user_model.get_max_users(),
        "current_users": user_model.get_user_count()
    }


@router.get("/check_auth")
def check_authentication(request: Request):
    """Is this browser logged in? Asking doesn't count as activity, and can't revive a session that ran out."""
    username = check_auth(request, check_inactivity=False)
    return {"authenticated": bool(username), "username": username}


@router.post("/activity")
def record_activity(request: Request):
    """Record user activity to refresh session timeout."""
    username = check_auth(request, check_inactivity=True)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized or Session Expired")
    return {"status": "ok"}


@router.post("/signup")
def signup(request: Request, response: Response, req: AuthRequest, user_model = Depends(get_user_model)):
    """Create the owner account (only while there is none)."""
    if user_model.get_user_count() >= user_model.get_max_users():
        raise HTTPException(status_code=403, detail="Cannot exceed maximum number of users.")
    if len(req.password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Use a password of at least {MIN_PASSWORD} characters.")
    success, message = user_model.create_user(req.username, req.password)
    if success:
        _start_session(request, response, req.username.lower().strip())
        return {"status": "success", "message": "Account created."}
    else:
        status = 403 if "maximum" in message else 400
        raise HTTPException(status_code=status, detail=message)


@router.post("/login")
def login(request: Request, response: Response, req: AuthRequest, user_model = Depends(get_user_model)):
    """Authenticate user and create session."""
    _too_many(request)
    if user_model.verify_user(req.username, req.password):
        login_limiter.succeeded(_client_ip(request))
        _start_session(request, response, req.username.lower().strip())
        return {"status": "success", "message": "Login successful"}
    login_limiter.failed(_client_ip(request))
    raise HTTPException(status_code=401, detail="Invalid username or password")


@router.post("/logout")
def logout(response: Response, request: Request):
    """End this session."""
    auth_sessions.end(session_token(request))
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"status": "ok", "message": "Logged out"}


@router.get("/account")
def account(request: Request):
    """The logged-in account (Settings -> General -> Account)."""
    return {"username": check_auth(request)}


@router.post("/account/password")
def change_password(request: Request, req: PasswordChange, user_model = Depends(get_user_model)):
    """Change the password (needs the current one). Every other session of the account ends."""
    username = check_auth(request)
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized or Session Expired")
    _too_many(request)
    if not user_model.verify_user(username, req.current_password):
        login_limiter.failed(_client_ip(request))
        raise HTTPException(status_code=400, detail="The current password is wrong.")
    if len(req.new_password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Use a password of at least {MIN_PASSWORD} characters.")
    user_model.set_password(username, req.new_password)
    ended = auth_sessions.end_all(username, keep=session_token(request))
    return {"status": "ok", "message": "Password changed.", "other_sessions_ended": ended}


def set_user_model(user_model):
    """Set the user model instance (called from web_server.py)."""
    global _user_model
    _user_model = user_model
