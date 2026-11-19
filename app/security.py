from __future__ import annotations

import base64
import hashlib
import time

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import settings

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="ft-session")
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("enc:" + settings.secret_key).encode()).digest()))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def make_session(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.session_max_age_seconds)
    except BadSignature:
        return None
    return data.get("uid")


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


class RateLimiter:
    """In-memory sliding window limiter (single container, single user: no shared state needed)."""

    def __init__(self, max_hits: int, window: int):
        self.max_hits, self.window = max_hits, window
        self.hits: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - self.window
        self.hits[key] = [t for t in self.hits.get(key, []) if t > cutoff]
        return self.hits[key]

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_hits

    def hit(self, key: str) -> None:
        self._recent(key).append(time.time())

    def allow(self, key: str) -> bool:
        """Count a hit and say whether it is within the limit."""
        if self.blocked(key):
            return False
        self.hit(key)
        return True

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


login_limiter = RateLimiter(5, 300)  # 5 failed logins / 5 min / client
heavy_limiter = RateLimiter(40, 60)  # uploads, scraping, ChatGPT calls: 40 / min
