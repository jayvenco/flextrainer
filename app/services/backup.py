"""Consistent backups (SQLite online backup API) with export, import and retention."""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from ..config import BACKUP_DIR, DATA_DIR, settings
from ..database import SessionLocal, engine
from . import store

DB_FILE = DATA_DIR / "app.db"


class BackupError(Exception):
    pass


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def create_backup(kind: str = "manual") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{kind}-{_stamp()}.zip"
    n = 1
    while target.exists():
        n += 1
        target = BACKUP_DIR / f"{kind}-{_stamp()}-{n}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "app.db"
        src = sqlite3.connect(DB_FILE)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "app.db")
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "app": settings.app_name,
                        "version": settings.version,
                        "kind": kind,
                        "created": dt.datetime.now().isoformat(timespec="seconds"),
                    }
                ),
            )
    return target


def list_backups() -> list[dict]:
    out = []
    for f in sorted(BACKUP_DIR.glob("*.zip"), reverse=True):
        st = f.stat()
        out.append(
            {
                "name": f.name,
                "size": st.st_size,
                "created": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return out


def safe_path(name: str) -> Path:
    p = BACKUP_DIR / Path(name).name
    if not p.is_file() or p.suffix != ".zip":
        raise BackupError("Backup not found.")
    return p


def prune(keep: int) -> None:
    """Retention applies to automatic and manual backups; safety copies are left alone."""
    files = sorted(
        (f for f in BACKUP_DIR.glob("*.zip") if not f.name.startswith("before-restore")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in files[max(1, keep) :]:
        old.unlink(missing_ok=True)


def restore(data: bytes) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BackupError("That file is not a valid backup archive.") from exc
    names = zf.namelist()
    if "app.db" not in names or "manifest.json" not in names:
        raise BackupError("That archive is not a FlexTrainer backup.")
    if any(n.startswith("/") or ".." in Path(n).parts for n in names):
        raise BackupError("Unsafe archive.")
    with tempfile.TemporaryDirectory() as tmp:
        cand = Path(tmp) / "app.db"
        cand.write_bytes(zf.read("app.db"))
        try:
            check = sqlite3.connect(cand)
            ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            has = check.execute("SELECT name FROM sqlite_master WHERE name='exams'").fetchone()
            check.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError("The database in the archive is damaged.") from exc
        if not ok or not has:
            raise BackupError("The database in the archive is damaged or from another app.")
        create_backup("before-restore")
        engine.dispose()
        src = sqlite3.connect(cand)
        dst = sqlite3.connect(DB_FILE)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    engine.dispose()


def run_due() -> Path | None:
    """Called periodically by the scheduler; creates an automatic backup when one is due."""
    with SessionLocal() as db:
        cfg = store.backup_config(db)
    if not cfg["enabled"]:
        return None
    interval = dt.timedelta(days=7 if cfg["schedule"] == "weekly" else 1)
    autos = sorted(BACKUP_DIR.glob("auto-*.zip"), key=lambda f: f.stat().st_mtime, reverse=True)
    if autos and dt.datetime.now() - dt.datetime.fromtimestamp(autos[0].stat().st_mtime) < interval:
        return None
    path = create_backup("auto")
    prune(cfg["keep"])
    return path
