from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
BACKUP_DIR = DATA_DIR / "backup"
LOG_DIR = DATA_DIR / "logs"
for _d in (DATA_DIR, BACKUP_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SECRET_KEY_FILE = DATA_DIR / ".secret_key"

THEMES = {
    "daylight": "Daylight (light)",
    "sand": "Sand (light)",
    "midnight": "Midnight (dark)",
    "nord": "Nord (dark)",
    "forest": "Forest (dark)",
}

FONTS = {
    "inter": ("Inter", "'Inter', system-ui, sans-serif"),
    "roboto": ("Roboto", "'Roboto', system-ui, sans-serif"),
    "open-sans": ("Open Sans", "'Open Sans', system-ui, sans-serif"),
    "lato": ("Lato", "'Lato', system-ui, sans-serif"),
    "system": ("System UI", "system-ui, -apple-system, 'Segoe UI', sans-serif"),
}

# Text size as a percentage of the browser default (applied to the whole app).
FONT_SIZES = {85: "Small", 100: "Normal", 115: "Large", 130: "Larger", 150: "Extra large", 175: "Huge"}


class ConfigError(Exception):
    pass


def _load_or_create_secret_key() -> str:
    """Random secret generated on first start and kept in the data volume, so sessions survive updates."""
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)
    SECRET_KEY_FILE.chmod(0o600)
    return key


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() != "" else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if value == "" else value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, errors: list[str]) -> int:
    raw = _env(name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be a whole number (got {raw!r}).")
        return default
    if value < minimum:
        errors.append(f"{name} must be at least {minimum} (got {value}).")
        return default
    return value


def _base_path(raw: str, errors: list[str]) -> str:
    if not raw:
        return ""
    path = urlparse(raw).path if "://" in raw else raw
    path = "/" + path.strip("/")
    if path == "/":
        return ""
    if not all(c.isalnum() or c in "-_/." for c in path):
        errors.append(f"BASE_URL path {path!r} contains unsupported characters.")
    return path


class Settings:
    """All configuration, read once at startup and validated with clear error messages."""

    app_name = "FlexTrainer"
    version = __version__
    github_repo = "jayvenco/flextrainer"
    database_url = f"sqlite:///{DATA_DIR / 'app.db'}"
    session_cookie_name = "ft_session"
    session_max_age_seconds = 60 * 60 * 24 * 14
    default_username = "admin"
    default_password = "admin"

    def __init__(self) -> None:
        errors: list[str] = []
        self.secret_key = _load_or_create_secret_key()
        self.default_theme = _env("DEFAULT_THEME", "midnight")
        if self.default_theme not in THEMES:
            errors.append(f"DEFAULT_THEME must be one of {', '.join(THEMES)} (got {self.default_theme!r}).")
            self.default_theme = "midnight"
        self.base_path = _base_path(_env("BASE_URL"), errors)
        self.cookie_secure = _env_bool("COOKIE_SECURE", False)
        self.openai_api_key_env = _env("OPENAI_API_KEY")
        self.openai_model_default = _env("OPENAI_MODEL", "gpt-4o-mini")
        self.openai_base_url = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if not self.openai_base_url.startswith(("http://", "https://")):
            errors.append("OPENAI_BASE_URL must start with http:// or https://.")
        self.backup_enabled_default = _env_bool("BACKUP_ENABLED", True)
        self.backup_keep_default = _env_int("BACKUP_KEEP", 7, 1, errors)
        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.")
            self.log_level = "INFO"
        self.update_check = _env_bool("UPDATE_CHECK", True)
        self.allow_private_scrape = _env_bool("ALLOW_PRIVATE_SCRAPE", False)
        _env_int("PUID", 99, 0, errors)
        _env_int("PGID", 100, 0, errors)
        self.errors = errors


settings = Settings()


def validate_or_exit() -> None:
    if settings.errors:
        sys.stderr.write("FlexTrainer cannot start because of invalid configuration:\n")
        for e in settings.errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write("Fix the environment variables (see .env.example) and start again.\n")
        raise SystemExit(2)
