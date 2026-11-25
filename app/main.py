from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import logging_setup
from .config import BASE_DIR, settings, validate_or_exit

validate_or_exit()
logging_setup.setup()

from .database import Base, SessionLocal, engine, migrate  # noqa: E402
from .deps import NeedLogin, NeedPasswordChange  # noqa: E402
from .models import User  # noqa: E402
from .routers import pages, ui  # noqa: E402
from .security import hash_password  # noqa: E402
from .services import backup  # noqa: E402

log = logging.getLogger("flextrainer")
B = settings.base_path


def seed_admin() -> None:
    with SessionLocal() as db:
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=settings.default_username,
                    password_hash=hash_password(settings.default_password),
                    must_change_password=True,
                    theme=settings.default_theme,
                )
            )
            db.commit()
            log.info("Created default account admin/admin (password change is required at first login)")


async def backup_scheduler() -> None:
    while True:
        try:
            path = await asyncio.to_thread(backup.run_due)
            if path:
                log.info("Automatic backup created: %s", path.name)
        except Exception:  # noqa: BLE001
            log.exception("Automatic backup failed")
        await asyncio.sleep(900)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    migrate()
    seed_admin()
    log.info("FlexTrainer %s started (base path %r)", settings.version, B or "/")
    task = asyncio.create_task(backup_scheduler())
    yield
    task.cancel()
    engine.dispose()
    log.info("FlexTrainer stopped")


core = FastAPI(title=settings.app_name, version=settings.version, docs_url=None, redoc_url=None, openapi_url=None)


@core.middleware("http")
async def protect(request: Request, call_next):
    # CSRF: state-changing requests must come from the same origin (behind a proxy: X-Forwarded-Host).
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if origin.split("://", 1)[-1] != host.split(",")[0].strip():
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@core.exception_handler(NeedLogin)
async def _need_login(_r: Request, _e: NeedLogin):
    return RedirectResponse(B + "/login", status_code=303)


@core.exception_handler(NeedPasswordChange)
async def _need_pw(_r: Request, _e: NeedPasswordChange):
    return RedirectResponse(B + "/account/password", status_code=303)


def health():
    return {"status": "ok", "version": settings.version}


def ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


core.get("/health", include_in_schema=False)(health)
core.get("/ready", include_in_schema=False)(ready)
core.include_router(pages.router)
core.include_router(ui.router)
core.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
core.router.lifespan_context = lifespan

if B:
    # Behind a reverse proxy under a sub-path (BASE_URL=/flextrainer). /health stays reachable at the root
    # for the Docker healthcheck.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.get("/health", include_in_schema=False)(health)
    app.get("/ready", include_in_schema=False)(ready)
    app.mount(B, core)
else:
    app = core
