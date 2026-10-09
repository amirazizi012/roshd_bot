"""کلاینت سبک برای Bot API بله (سازگار با ساختار API تلگرام) — فقط با httpx."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger("bale")


class BaleError(Exception):
    pass


def inline(rows):
    """rows: [[(متن, callback_data), ...], ...]"""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def reply_kb(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True}


class Bale:
    def __init__(self, token: str, base: str = "https://tapi.bale.ai", proxy: str | None = None,
                 trust_env: bool = False):
        """trust_env=False یعنی پروکسی/فیلترشکنِ تنظیم‌شده در سیستم نادیده گرفته می‌شود (بله از داخل ایران مستقیم در دسترس است)."""
        self.api = f"{base}/bot{token}"
        self.file_api = f"{base}/file/bot{token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=30.0), proxy=proxy or None, trust_env=trust_env)

    async def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        try:
            r = await self.http.post(f"{self.api}/{method}", json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise BaleError(f"{method}: {type(e).__name__}: {e}") from e
        if not data.get("ok"):
            raise BaleError(f"{method}: {data.get('description')} ({data.get('error_code')})")
        return data["result"]

    async def get_updates(self, offset=None, timeout=25):
        return await self.call("getUpdates", offset=offset, timeout=timeout)

    async def send_message(self, chat_id, text, reply_markup=None):
        return await self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                               reply_markup=reply_markup)

    async def answer_callback(self, cb_id, text=None):
        try:
            await self.call("answerCallbackQuery", callback_query_id=cb_id, text=text)
        except BaleError as e:
            log.debug("answerCallbackQuery failed: %s", e)

    async def send_photo(self, chat_id, file_id, caption=None):
        return await self.call("sendPhoto", chat_id=chat_id, photo=file_id, caption=caption)

    async def send_document(self, chat_id, file_id, caption=None):
        return await self.call("sendDocument", chat_id=chat_id, document=file_id, caption=caption)

    async def send_file(self, chat_id, path: Path, kind="document", caption=None):
        """آپلود فایل از دیسک (multipart). kind: photo | document."""
        method, field = ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")
        try:
            with open(path, "rb") as f:
                r = await self.http.post(f"{self.api}/{method}", data={"chat_id": str(chat_id), **({"caption": caption} if caption else {})},
                                         files={field: (Path(path).name, f)})
            data = r.json()
        except (httpx.HTTPError, ValueError, OSError) as e:
            raise BaleError(f"{method}: {e}") from e
        if not data.get("ok"):
            raise BaleError(f"{method}: {data.get('description')} ({data.get('error_code')})")
        return data["result"]

    async def download(self, file_id: str, dest: Path) -> bool:
        """فایل را روی دیسک ذخیره می‌کند؛ اگر شکست خورد False (file_id همچنان در دیتابیس می‌ماند)."""
        try:
            info = await self.call("getFile", file_id=file_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with self.http.stream("GET", f"{self.file_api}/{info['file_path']}") as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
            return True
        except (BaleError, httpx.HTTPError, OSError, KeyError) as e:
            log.warning("download failed for %s: %s", file_id, e)
            return False
