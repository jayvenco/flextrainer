"""Non-intrusive 'update available' check against GitHub releases (cached, failures are silent)."""

from __future__ import annotations

import re
import threading
import time

import httpx

from ..config import settings

_cache: dict = {"at": 0.0, "latest": None}
_lock = threading.Lock()


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def latest_release() -> str | None:
    if not settings.update_check:
        return None
    with _lock:
        if time.time() - _cache["at"] < 6 * 3600:
            return _cache["latest"]
    latest = None
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{settings.github_repo}/releases/latest",
            timeout=4,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "FlexTrainer"},
        )
        if r.status_code == 200:
            latest = r.json().get("tag_name")
    except (httpx.HTTPError, ValueError):
        latest = None
    with _lock:
        _cache.update(at=time.time(), latest=latest)
    return latest


def update_info() -> dict:
    latest = latest_release()
    newer = bool(latest) and _ver(latest) > _ver(settings.version)
    return {
        "current": settings.version,
        "latest": latest,
        "available": newer,
        "url": f"https://github.com/{settings.github_repo}/releases/latest",
    }
