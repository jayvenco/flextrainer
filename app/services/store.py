"""Tiny key/value settings store (backup config, ChatGPT key and model)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Setting
from ..security import decrypt, encrypt


def get(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def put(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def backup_config(db: Session) -> dict:
    enabled = get(db, "backup_enabled", "1" if settings.backup_enabled_default else "0") == "1"
    try:
        keep = max(1, int(get(db, "backup_keep", str(settings.backup_keep_default))))
    except ValueError:
        keep = settings.backup_keep_default
    return {"enabled": enabled, "keep": keep, "schedule": "weekly"}


def openai_config(db: Session) -> dict:
    """The ChatGPT key comes from the settings page (stored encrypted) or the OPENAI_API_KEY variable."""
    stored = get(db, "openai_key")
    key = decrypt(stored) if stored else ""
    key = key or settings.openai_api_key_env
    return {
        "key": key,
        "model": get(db, "openai_model") or settings.openai_model_default,
        "base_url": settings.openai_base_url,
        "configured": bool(key),
    }


def set_openai_key(db: Session, key: str) -> None:
    put(db, "openai_key", encrypt(key) if key else "")
