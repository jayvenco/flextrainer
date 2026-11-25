"""Question pictures: validation, size limits and storage. Only PNG, JPEG, GIF and WebP are accepted (no SVG)."""

from __future__ import annotations

import hashlib
import io
import json
import re

from sqlalchemy.orm import Session

from ..models import Image, Question

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1600  # larger pictures are scaled down to keep the database small
MIN_SIDE = 40  # smaller pictures are bullets, rules or icons
MIN_BYTES_NO_PIL = 3000
MAX_PER_QUESTION = 6
MARKER_RE = re.compile(r"⟦img:([A-Za-z0-9_]+)⟧")


def marker(key: str) -> str:
    return f"⟦img:{key}⟧"


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text)


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalise(data: bytes, keep_small: bool = False) -> tuple[bytes, str, int, int] | None:
    """Return (bytes, mime, width, height) or None when the picture is unusable or decorative."""
    if not data or len(data) > MAX_INPUT_BYTES:
        return None
    mime = sniff(data)
    if not mime:
        return None
    try:
        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(data))
        im.load()
    except ImportError:  # pragma: no cover - Pillow is a dependency, but stay usable without it
        return (data, mime, 0, 0) if keep_small or len(data) >= MIN_BYTES_NO_PIL else None
    except Exception:  # noqa: BLE001 - corrupt or exotic image
        return None
    w, h = im.size
    if not keep_small and (w < MIN_SIDE or h < MIN_SIDE):
        return None
    if max(w, h) <= MAX_SIDE:
        return data, mime, w, h
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    if im.mode in ("RGBA", "LA", "P"):
        im.convert("RGBA").save(out, "PNG", optimize=True)
        mime = "image/png"
    else:
        im.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
        mime = "image/jpeg"
    return out.getvalue(), mime, im.size[0], im.size[1]


def store_image(db: Session, exam_id: int, data: bytes, keep_small: bool = False) -> Image | None:
    norm = normalise(data, keep_small)
    return save(db, exam_id, norm) if norm else None


def save(db: Session, exam_id: int, norm: tuple[bytes, str, int, int]) -> Image:
    blob, mime, w, h = norm
    sha = hashlib.sha1(blob).hexdigest()
    row = db.query(Image).filter(Image.exam_id == exam_id, Image.sha1 == sha).first()
    if row:
        return row
    row = Image(exam_id=exam_id, sha1=sha, mime=mime, width=w, height=h, data=blob)
    db.add(row)
    db.flush()
    return row


def shas(db: Session, ids: list[int]) -> list[str]:
    if not ids:
        return []
    return [s for (s,) in db.query(Image.sha1).filter(Image.id.in_(ids))]


def prune(db: Session, exam_id: int) -> None:
    """Delete pictures no question of the exam uses any more."""
    used: set[int] = set()
    for (raw,) in db.query(Question.images_json).filter(Question.exam_id == exam_id):
        used.update(json.loads(raw or "[]"))
    for (iid,) in db.query(Image.id).filter(Image.exam_id == exam_id):
        if iid not in used:
            db.query(Image).filter(Image.id == iid).delete()
    db.commit()
