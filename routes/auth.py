# This file is: ./routes/auth.py

"""
Authentication routes.
Handles user signup, login, logout, and session management.
"""

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel
from helpers import sign_token, check_auth, auth_tokens
from config import AUTH_COOKIE_NAME

router = APIRouter(prefix="/api", tags=["auth"])

# Global reference to user_model (will be set by web_server.py)
_user_model = None

def get_user_model():
    """Dependency function to get user_model."""
    return _user_model


class AuthRequest(BaseModel):
    """Authentication request model."""
    username: str
    password: str


@router.get("/max_users")
def get_max_user_info(user_model = Depends(get_user_model)):
    """Get user count and limits."""
    return {
        "max_users": user_model.get_max_users(),
        "current_users": user_model.get_user_count()
    }


@router.get("/check_auth")
def check_authentication(request: Request):
    """Check if user is authenticated without timeout enforcement."""
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
def signup(response: Response, req: AuthRequest, user_model = Depends(get_user_model)):
    """Create new user account."""
    success, message = user_model.create_user(req.username, req.password)
    if success:
        token = sign_token(req.username)
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite='lax',
        )
        return {"status": "success", "message": "Account created."}
    else:
        status = 403 if "maximum" in message else 400
        raise HTTPException(status_code=status, detail=message)


@router.post("/login")
def login(response: Response, req: AuthRequest, user_model = Depends(get_user_model)):
    """Authenticate user and create session."""
    if user_model.verify_user(req.username, req.password):
        token = sign_token(req.username)
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite='lax',
        )
        return {"status": "success", "message": "Login successful"}
    else:
        raise HTTPException(status_code=401, detail="Invalid username or password")


@router.post("/logout")
def logout(response: Response, request: Request):
    """End user session."""
    # Remove from server-side session storage
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        try:
            parts = token.split('|')
            if len(parts) == 3:
                signature = parts[2]
                if signature in auth_tokens:
                    del auth_tokens[signature]
        except:
            pass
    
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"status": "ok", "message": "Logged out"}


def set_user_model(user_model):
    """Set the user model instance (called from web_server.py)."""
    global _user_model
    _user_model = user_model