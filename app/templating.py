from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, FONT_SIZES, FONTS, THEMES, settings

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals.update(
    THEMES=THEMES,
    FONTS=FONTS,
    FONT_SIZES=FONT_SIZES,
    app_name=settings.app_name,
    app_version=settings.version,
    base=settings.base_path,
)


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = dict(ctx or {})
    user = ctx.get("user")
    ctx.setdefault("theme", user.theme if user else settings.default_theme)
    ctx.setdefault("font", user.font if user else "inter")
    ctx.setdefault("font_size", user.font_size if user else 100)
    return templates.TemplateResponse(request, name, ctx, status_code=status)
