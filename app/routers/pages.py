from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import logging_setup
from ..config import settings
from ..database import get_db
from ..deps import client_ip, page_user, session_user
from ..models import Exam, PracticeSession, User
from ..security import hash_password, login_limiter, make_session, verify_password
from ..services import backup, exams, store, updates
from ..templating import render

router = APIRouter(include_in_schema=False)
B = settings.base_path


def _secure(request: Request) -> bool:
    return settings.cookie_secure or request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _login_redirect(request: Request, user: User, to: str) -> RedirectResponse:
    resp = RedirectResponse(B + to, status_code=303)
    resp.set_cookie(
        settings.session_cookie_name,
        make_session(user.id),
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        max_age=settings.session_max_age_seconds,
        path=B or "/",
    )
    return resp


# ---------- auth ----------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if session_user(request, db):
        return RedirectResponse(B + "/", status_code=303)
    return render(request, "login.html", {"error": None, "user": None})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    key = client_ip(request)
    if login_limiter.blocked(key):
        return render(
            request, "login.html", {"error": "Too many attempts. Try again in a few minutes.", "user": None}, status=429
        )
    user = db.query(User).filter(User.username == username.strip()).first()
    if not user or not verify_password(password, user.password_hash):
        login_limiter.hit(key)
        return render(request, "login.html", {"error": "Wrong username or password.", "user": None}, status=401)
    login_limiter.reset(key)
    return _login_redirect(request, user, "/account/password" if user.must_change_password else "/")


@router.post("/logout")
def logout():
    resp = RedirectResponse(B + "/login", status_code=303)
    resp.delete_cookie(settings.session_cookie_name, path=B or "/")
    return resp


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, db: Session = Depends(get_db)):
    user = session_user(request, db)
    if not user:
        return RedirectResponse(B + "/login", status_code=303)
    return render(request, "password.html", {"user": user, "error": None, "forced": user.must_change_password})


@router.post("/account/password", response_class=HTMLResponse)
def password_change(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return RedirectResponse(B + "/login", status_code=303)
    error = None
    if not verify_password(current, user.password_hash):
        error = "Current password is wrong."
    elif len(new) < 8:
        error = "The new password must be at least 8 characters."
    elif new != confirm:
        error = "The two new passwords do not match."
    elif new == "admin":
        error = "Choose a password other than the default."
    if error:
        return render(
            request, "password.html", {"user": user, "error": error, "forced": user.must_change_password}, status=400
        )
    user.password_hash = hash_password(new)
    user.must_change_password = False
    db.commit()
    return _login_redirect(request, user, "/")


# ---------- app pages ----------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    cards = [exams.exam_stats(db, e) for e in db.query(Exam).order_by(Exam.created_at.desc())]
    return render(
        request,
        "home.html",
        {"user": user, "active": "home", "cards": cards, "gpt": store.openai_config(db)["configured"]},
    )


@router.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_page(exam_id: int, request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    exam = db.get(Exam, exam_id)
    if not exam:
        raise HTTPException(404, "Exam not found")
    return render(
        request,
        "exam.html",
        {
            "user": user,
            "active": "home",
            "exam": exam,
            "stats": exams.exam_stats(db, exam),
            "questions": [exams.question_json(q) for q in exam.questions],
            "sources": exam.sources,
            "gpt": store.openai_config(db)["configured"],
            "job": exams.verify_status(exam.id),
        },
    )


@router.get("/practice/{session_id}", response_class=HTMLResponse)
def practice_page(session_id: int, request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    s = db.get(PracticeSession, session_id)
    if not s:
        raise HTTPException(404, "Practice session not found")
    return render(request, "practice.html", {"user": user, "active": "home", "session": s, "exam": s.exam})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    cfg = store.openai_config(db)
    stored = bool(store.get(db, "openai_key"))
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "active": "settings",
            "backups": backup.list_backups(),
            "bcfg": store.backup_config(db),
            "gpt": {
                "configured": cfg["configured"],
                "model": cfg["model"],
                "from_env": cfg["configured"] and not stored,
                "base_url": cfg["base_url"],
            },
        },
    )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, level: str = "", user: User = Depends(page_user)):
    lines = logging_setup.tail(600)
    if level.upper() in {"WARNING", "ERROR"}:
        wanted = {"ERROR"} if level.upper() == "ERROR" else {"WARNING", "ERROR"}
        lines = [ln for ln in lines if any(f"level={lv} " in ln for lv in wanted)]
    return render(request, "logs.html", {"user": user, "active": "logs", "lines": lines[-400:], "level": level.upper()})


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request, user: User = Depends(page_user)):
    from ..config import BASE_DIR

    try:
        changelog = (BASE_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        changelog = ""
    return render(
        request, "about.html", {"user": user, "active": "about", "changelog": changelog, "upd": updates.update_info()}
    )


# ---------- PWA ----------


@router.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": settings.app_name,
        "short_name": settings.app_name,
        "start_url": B + "/",
        "scope": B + "/",
        "display": "standalone",
        "background_color": "#0f1420",
        "theme_color": "#2563eb",
        "description": "Practice exams from your own question material.",
        "icons": [
            {"src": B + "/static/images/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": B + "/static/images/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(json.dumps(data), media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    js = (
        "const C='ft-static-" + settings.version + "';\n"
        "self.addEventListener('install',e=>{self.skipWaiting();});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==C)"
        ".map(x=>caches.delete(x)))).then(()=>self.clients.claim()));});\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);\n"
        " if(e.request.method!=='GET'||u.origin!==location.origin||!u.pathname.startsWith('"
        + B
        + "/static/'))return;\n"
        " e.respondWith(caches.open(C).then(c=>c.match(e.request).then(h=>h||fetch(e.request).then(r=>"
        "{if(r.ok)c.put(e.request,r.clone());return r;}))));});\n"
    )
    return Response(
        js, media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": B + "/"}
    )
