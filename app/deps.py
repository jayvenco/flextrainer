from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User
from .security import heavy_limiter, read_session


class NeedLogin(Exception):
    pass


class NeedPasswordChange(Exception):
    pass


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def session_user(request: Request, db: Session) -> User | None:
    uid = read_session(request.cookies.get(settings.session_cookie_name))
    return db.get(User, uid) if uid else None


def page_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = session_user(request, db)
    if not user:
        raise NeedLogin()
    if user.must_change_password:
        raise NeedPasswordChange()
    return user


def ui_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Cookie-only JSON endpoints used by the web UI. There is no public/token API."""
    user = session_user(request, db)
    if not user:
        raise HTTPException(401, "Not signed in")
    if user.must_change_password:
        raise HTTPException(403, "Change the default password first")
    return user


def throttled(request: Request, user: User = Depends(ui_user)) -> User:
    if not heavy_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests. Wait a moment and try again.")
    return user
