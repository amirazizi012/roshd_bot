"""اجرا:  python -m roshd_bot.main          (اجرای ربات)
         python -m roshd_bot.main --check  (فقط تست اتصال و توکن)"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys

from .bale import Bale, BaleError
from .bot import App
from .config import load_config
from .backup import make_backup
from .db import Repo
from .panel import start_panel
from .timeutil import utcnow

log = logging.getLogger("roshd")


async def backup_loop(cfg, bale, repo):
    """هر ۲۴ ساعت یک بکاپ می‌سازد و برای همهٔ مدیران در بله می‌فرستد."""
    await asyncio.sleep(120)
    while True:
        try:
            path = make_backup(cfg, repo, utcnow())
            for admin in cfg.admin_ids:
                try:
                    await bale.send_file(admin, path, "document", "🗄 بکاپ خودکار دیتابیس تکالیف")
                except BaleError as e:
                    log.warning("backup send failed: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("backup failed")
        await asyncio.sleep(24 * 3600)


async def run(check_only: bool = False) -> None:
    cfg = load_config()
    if not cfg.token:
        sys.exit("BALE_TOKEN در فایل .env تنظیم نشده است.")
    if os.environ.get("DIAG_ON_START") == "1":      # خروجی عیب‌یابی اتصال داخل لاگ‌های سرور چاپ می‌شود
        from .diag import run as diag_run
        await diag_run()
    bale = Bale(cfg.token, cfg.api_base, proxy=cfg.bale_proxy, trust_env=cfg.use_system_proxy)
    while True:
        try:
            me = await bale.call("getMe")
            break
        except BaleError as e:
            if check_only:
                print(f"CONNECTION FAILED: {e}\nRun:  python -m roshd_bot.main --diag")
                sys.exit(1)
            log.warning("cannot reach Bale (%s). Retrying in 15s... (run --diag to investigate)", e)
            await asyncio.sleep(15)
    log.info("connected as @%s", me.get("username"))
    if check_only:
        print("OK:", me)
        return
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", cfg.lock_port))     # تا زنده بودنِ پردازش نگه داشته می‌شود
    except OSError:
        print("ANOTHER INSTANCE of the bot is already running on this computer. Close it first.")
        sys.exit(3)
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS خالی است؛ در ربات /myid را بزن و عدد را در .env بگذار.")
    repo = Repo(cfg.db_path)
    app = App(cfg, bale, repo)
    if cfg.panel_password:
        try:
            await start_panel(cfg, repo)
        except OSError as e:
            log.warning("پنل وب بالا نیامد (%s). معمولاً پورت %s اشغال است؛ در .env مقدار PANEL_PORT را عوض کن. "
                        "ربات بدون پنل ادامه می‌دهد.", e, cfg.panel_port)
    else:
        log.info("PANEL_PASSWORD تنظیم نشده؛ پنل وب غیرفعال است.")
    asyncio.create_task(backup_loop(cfg, bale, repo))
    offset = int(repo.meta_get("offset") or 0) or None
    while True:
        try:
            updates = await bale.get_updates(offset)
        except BaleError as e:
            log.warning("poll failed: %s", e)
            await asyncio.sleep(5)
            continue
        for u in updates:
            await app.handle_update(u)
            offset = u["update_id"] + 1
            repo.meta_set("offset", offset)


if __name__ == "__main__":
    if "--diag" in sys.argv:
        from .diag import run as diag_run
        asyncio.run(diag_run())
        sys.exit(0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):          # آدرس درخواست‌ها شامل توکن ربات است؛ هرگز در لاگ نیاید
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(run("--check" in sys.argv))
