"""پشتیبان‌گیری: کپی سازگار از دیتابیس (بدون توقف ربات) + zip. هر روز برای مدیران در بله فرستاده می‌شود."""
from __future__ import annotations

import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path


def make_backup(cfg, repo, now: datetime) -> Path:
    out = cfg.data_dir / "backups"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(cfg.tz).strftime("%Y-%m-%d_%H%M")
    db_copy = out / f"roshd-{stamp}.db"
    dst = sqlite3.connect(db_copy)
    with dst:
        repo.c.backup(dst)            # API رسمی SQLite؛ حتی وسط کار ربات سالم است
    dst.close()
    zpath = out / f"roshd-backup-{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy, "roshd.db")
    db_copy.unlink()
    for old in sorted(out.glob("roshd-backup-*.zip"))[:-14]:     # ۱۴ بکاپ آخر
        old.unlink()
    return zpath
