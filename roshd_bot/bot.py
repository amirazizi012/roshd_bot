"""منطق گفتگو: مسیر دانش‌آموز (ثبت‌نام، دیدن تکلیف، ارسال پاسخ) و مسیر مدیر (ساخت گروه/دانش‌آموز/تکلیف، گزارش)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from .bale import Bale, BaleError, inline, reply_kb
from .config import Config
from .db import Repo
from .backup import make_backup
from .export import LOGO, build_excel, build_zip
from .rules import EXCUSED, LATE, MISSING, ON_TIME, OPEN, STREAK_LEN, STREAK_POINTS
from .timeutil import (en, fa, fmt_dt, fmt_duration, from_db, jalali_str, parse_deadline, to_db, utcnow)

log = logging.getLogger("roshd")

B_MY, B_BADGE, B_HELP = "📚 تکالیف من", "🏅 نشان‌ها و امتیاز", "ℹ️ راهنما"
B_NEW, B_ALL, B_STU, B_GRP = "➕ تکلیف جدید", "📋 تکالیف", "👥 دانش‌آموزان", "🗂 گروه‌ها"
B_MSG, B_XLS = "📢 پیام همگانی", "📊 خروجی اکسل"
ALL_BUTTONS = {B_MY, B_BADGE, B_HELP, B_NEW, B_ALL, B_STU, B_GRP, B_MSG, B_XLS}
KIND_FA = {"regular": "تکلیف اصلی", "optional": "اختیاری", "bonus": "امتیازی"}
ICON = {ON_TIME: "✅", LATE: "⚠️", MISSING: "❌", OPEN: "⏳", EXCUSED: "🎓"}

HELP_STUDENT = (
    "راهنمای ربات تکالیف 📘\n\n"
    "• «📚 تکالیف من»: تکالیف را ببین و پاسخ بفرست.\n"
    "• پاسخ را به‌صورت عکس یا PDF بفرست؛ چند عکس هم می‌شود. در پایان «✅ ثبت نهایی» را بزن.\n"
    "• اگر بعد از مهلت بفرستی، با میزان تاخیر ثبت می‌شود. تا {grace} دقیقه بعد از مهلت، مهلت ارفاقی است.\n"
    "• ۲۴ ساعت بعد از مهلت، ارسال بسته می‌شود.\n"
    "• پاسخ را واضح، صاف، کامل و با نور خوب عکس بگیر؛ عکس تار ممکن است برگردانده شود.\n"
    "• هر {streak} تکلیف اصلیِ پشت‌سرهم که به‌موقع بفرستی، یک نشان تعهد و {pts} امتیاز می‌گیری. یک تاخیر وسط، شمارش را از نو می‌کند.\n"
    "• زمان ارسال تو، آخرین باری است که پاسخ را فرستاده‌ای.\n"
    "• تکالیف «اختیاری» و «امتیازی» روی نشان تعهد اثر منفی ندارند."
)
HELP_ADMIN = (
    "راهنمای مدیر 🛠\n\n"
    "• «🗂 گروه‌ها» و «👥 دانش‌آموزان»: ساخت گروه و ثبت دانش‌آموز (هر دانش‌آموز یک کد ورود می‌گیرد).\n"
    "• «➕ تکلیف جدید»: عنوان، توضیح، فایل، نوع، گروه‌های هدف و مهلت.\n"
    "• «📋 تکالیف»: گزارش هر تکلیف + بررسی تک‌تک دانش‌آموزان (دیدن پاسخ، بازخورد، ارسال مجدد، تمدید فردی، عذر موجه)، "
    "تمدید برای همه، ویرایش، دانلود zip پاسخ‌ها و بایگانی.\n"
    "• «📢 پیام همگانی»: پیام به همه یا گروه‌های انتخابی.  «📊 خروجی اکسل»: آمار کامل تکالیف.\n"
    "• /asstudent: دیدن ربات از نگاه دانش‌آموز (برای تست)، با /start برمی‌گردی.\n"
    "• /backup: بکاپ دیتابیس (هر روز هم خودکار می‌آید). /myid: شناسهٔ عددی بله‌ات. /cancel: انصراف از مرحلهٔ جاری."
)


def extract_file(m: dict):
    if m.get("photo"):
        p = m["photo"][-1]
        return {"kind": "photo", "file_id": p["file_id"], "name": "photo.jpg", "ok": True}
    d = m.get("document")
    if d:
        name = d.get("file_name") or "file"
        mime = (d.get("mime_type") or "").lower()
        ok = mime == "application/pdf" or mime.startswith("image/") or name.lower().endswith(
            (".pdf", ".jpg", ".jpeg", ".png", ".webp"))
        return {"kind": "document", "file_id": d["file_id"], "name": name, "ok": ok}
    return None


def msg_time(m: dict) -> datetime:
    """زمان واقعی پیام از خود بله؛ اگر عجیب بود زمان فعلی (تا تاخیرِ ربات به ضرر دانش‌آموز نشود)."""
    now = utcnow()
    ts = m.get("date")
    if isinstance(ts, (int, float)) and ts > 0:
        if ts > 1e12:
            ts /= 1000
        try:
            t = datetime.fromtimestamp(ts, timezone.utc)
            if abs((t - now).total_seconds()) < 2 * 86400:
                return t.replace(microsecond=0)
        except (OverflowError, OSError, ValueError):
            pass
    return now


class App:
    def __init__(self, cfg: Config, bale: Bale, repo: Repo):
        self.cfg, self.bale, self.repo = cfg, bale, repo
        self.student_mode: set = set()

    # ------------------------------------------------------------ utils
    def is_admin(self, chat) -> bool:
        return chat in self.cfg.admin_ids and chat not in self.student_mode

    def fmt(self, dt) -> str:
        return fmt_dt(dt, self.cfg.tz)

    def menu(self, chat):
        if self.is_admin(chat):
            return reply_kb([[B_NEW, B_ALL], [B_STU, B_GRP], [B_MSG, B_XLS], [B_HELP]])
        return reply_kb([[B_MY], [B_BADGE, B_HELP]])

    async def say(self, chat, text, kb=None):
        return await self.bale.send_message(chat, text, kb)

    async def say_long(self, chat, text, kb=None):
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3500:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur.strip():
            chunks.append(cur)
        for i, c in enumerate(chunks):
            await self.say(chat, c.rstrip(), kb if i == len(chunks) - 1 else None)

    async def edit_or_say(self, chat, mid, text, kb=None):
        try:
            await self.bale.edit_message_text(chat, mid, text, kb)
        except BaleError:
            await self.say(chat, text, kb)

    # ------------------------------------------------------------ dispatch
    async def handle_update(self, u: dict):
        try:
            if "message" in u:
                await self.on_message(u["message"])
            elif "callback_query" in u:
                await self.on_callback(u["callback_query"])
        except Exception:  # noqa: BLE001 — ربات نباید با یک خطا از کار بیفتد
            log.exception("update failed: %s", u.get("update_id"))
            chat = (u.get("message") or (u.get("callback_query") or {}).get("message") or {}).get("chat", {}).get("id")
            if chat:
                try:
                    await self.say(chat, "⚠️ مشکلی پیش آمد. دوباره امتحان کن؛ اگر ادامه داشت به مدیر خبر بده.")
                except BaleError:
                    pass

    async def on_message(self, m: dict):
        chat = m["chat"]["id"]
        if m["chat"].get("type", "private") != "private":
            return
        text = (m.get("text") or "").strip()
        if text.startswith("/") and await self.on_command(chat, text):
            return
        state, data = self.repo.get_session(chat)
        if text in ALL_BUTTONS:
            self.repo.clear_session(chat)
            state = ""
        if state:
            h = getattr(self, "st_" + state, None)
            if h and await h(chat, data, m, text):
                return
        await self.on_menu(chat, text)

    async def on_command(self, chat, text) -> bool:
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/myid":
            await self.say(chat, f"شناسهٔ عددی تو در بله:\n{chat}")
        elif cmd == "/start":
            self.student_mode.discard(chat)
            self.repo.clear_session(chat)
            await self.cmd_start(chat)
        elif cmd == "/cancel":
            self.repo.clear_session(chat)
            await self.say(chat, "انصراف داده شد ✅", self.menu(chat))
        elif cmd == "/help":
            await self.say(chat, self.help_text(chat), self.menu(chat))
        elif cmd == "/backup" and self.is_admin(chat):
            path = make_backup(self.cfg, self.repo, utcnow())
            await self.bale.send_file(chat, path, "document", "🗄 بکاپ دیتابیس")
        elif cmd == "/asstudent" and chat in self.cfg.admin_ids:
            self.student_mode.add(chat)
            self.repo.clear_session(chat)
            await self.cmd_start(chat)
        else:
            return False
        return True

    def help_text(self, chat):
        if self.is_admin(chat):
            return HELP_ADMIN
        return HELP_STUDENT.format(grace=fa(self.cfg.grace_minutes), streak=fa(STREAK_LEN), pts=fa(STREAK_POINTS))

    async def on_menu(self, chat, text):
        if self.is_admin(chat):
            actions = {B_NEW: self.admin_new_assignment, B_ALL: self.admin_assignments,
                       B_STU: self.admin_students, B_GRP: self.admin_groups,
                       B_MSG: self.admin_broadcast, B_XLS: self.admin_excel}
            if text in actions:
                return await actions[text](chat)
            if text == B_HELP:
                return await self.say(chat, HELP_ADMIN, self.menu(chat))
            return await self.say(chat, "از منوی پایین یکی را انتخاب کن 👇", self.menu(chat))
        st = self.repo.student_by_chat(chat)
        if not st:
            return await self.cmd_start(chat)
        if text == B_MY:
            return await self.student_assignments(chat, st)
        if text == B_BADGE:
            return await self.student_badges(chat, st)
        if text == B_HELP:
            return await self.say(chat, self.help_text(chat), self.menu(chat))
        await self.say(chat, "از منوی پایین یکی را انتخاب کن 👇", self.menu(chat))

    async def on_callback(self, cb: dict):
        msg = cb.get("message") or {}
        chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
        await self.bale.answer_callback(cb["id"])
        kind, _, arg = (cb.get("data") or "").partition(":")
        h = getattr(self, "cb_" + kind, None)
        if h and chat is not None:
            await h(chat, mid, arg)

    # ------------------------------------------------------------ start / registration
    async def cmd_start(self, chat):
        if self.is_admin(chat):
            return await self.say(
                chat, f"سلام مدیر عزیز 👋\nپنل مدیریت ربات تکالیف «{self.cfg.org_name}»", self.menu(chat))
        st = self.repo.student_by_chat(chat)
        if st:
            return await self.say(chat, f"سلام {st['full_name']} 👋\nخوش برگشتی!", self.menu(chat))
        groups = self.repo.groups_with_open_slots()
        if not groups:
            return await self.say(chat, "سلام 👋\nهنوز گروهی برای ثبت‌نام آماده نیست. به مدیر مجموعه خبر بده.")
        await self.send_logo(chat)
        await self.say(
            chat, f"سلام 👋\nبه ربات تکالیف «{self.cfg.org_name}» خوش آمدی.\nاول گروه خودت را انتخاب کن:",
            inline([[(g["name"], f"rg:{g['id']}")] for g in groups]))

    async def cb_rg(self, chat, mid, arg):
        if self.repo.student_by_chat(chat) or self.is_admin(chat):
            return
        members = self.repo.unlinked_members(int(arg))
        if not members:
            return await self.say(chat, "در این گروه نام ثبت‌نام‌نشده‌ای نمانده. اگر اشتباه است به مدیر خبر بده.")
        rows = [[(s["full_name"], f"rs:{s['id']}")] for s in members]
        await self.edit_or_say(chat, mid, "اسم خودت را انتخاب کن:", inline(rows))

    async def cb_rs(self, chat, mid, arg):
        if self.repo.student_by_chat(chat) or self.is_admin(chat):
            return
        s = self.repo.student(int(arg))
        if not s or s["chat_id"]:
            return await self.say(chat, "این نام قبلاً ثبت شده است. به مدیر خبر بده.")
        self.repo.set_session(chat, "reg_code", {"sid": s["id"], "tries": 0})
        await self.say(chat, f"{s['full_name']} عزیز، کد ورودی را که مدیر به تو داده بفرست 🔑")

    async def st_reg_code(self, chat, data, m, text):
        s = self.repo.student(data["sid"])
        if s and not s["chat_id"] and en(text).strip() == s["code"]:
            self.repo.link_student(s["id"], chat)
            self.repo.clear_session(chat)
            await self.say(chat, f"✅ ثبت‌نام شد. خوش آمدی {s['full_name']}!\nاز منوی پایین شروع کن 👇", self.menu(chat))
            return True
        tries = data.get("tries", 0) + 1
        if tries >= 5:
            self.repo.clear_session(chat)
            await self.say(chat, "تعداد تلاش‌ها زیاد شد. با /start دوباره شروع کن یا از مدیر کد بگیر.")
        else:
            self.repo.set_session(chat, "reg_code", {**data, "tries": tries})
            await self.say(chat, "کد درست نیست. دوباره بفرست (یا /start برای شروع دوباره).")
        return True

    # ------------------------------------------------------------ student: assignments
    def status_line(self, o, dl, now, can_send=True):
        if o.status == ON_TIME:
            return ("🟡 ارسال شد؛ کمی بعد از مهلت بود ولی در مهلت ارفاقی است و به‌موقع حساب می‌شود"
                    if o.in_grace else "✅ ارسال شده — به‌موقع")
        if o.status == LATE:
            return f"⚠️ ارسال شده با {fmt_duration(o.late_seconds)} تاخیر"
        if o.status == EXCUSED:
            return "🎓 عذر تو پذیرفته شده"
        if o.status == MISSING:
            if not can_send:
                return "❌ مهلت ارسال تمام شده و پاسخی ثبت نشده. اگر عذری داری به مدیر بگو."
            return "❌ مهلت گذشته و پاسخی ثبت نشده؛ هنوز می‌توانی بفرستی (با تاخیر ثبت می‌شود)"
        if now > dl:
            left = dl + timedelta(minutes=self.cfg.grace_minutes) - now
            return f"⏳ مهلت اصلی تمام شده؛ {fmt_duration(left.total_seconds())} مهلت ارفاقی داری"
        return f"⏳ هنوز ارسال نکرده‌ای — {fmt_duration((dl - now).total_seconds())} مانده"

    async def student_assignments(self, chat, st):
        now = utcnow()
        items = self.repo.assignments_for(st["id"])[:20]
        if not items:
            return await self.say(chat, "فعلاً تکلیفی برای تو ثبت نشده 🌿", self.menu(chat))
        rows = []
        for a in items:
            o, _, _ = self.repo.evaluate(a, st["id"], now)
            tag = "" if a["kind"] == "regular" else f" ({KIND_FA[a['kind']]})"
            rows.append([(f"{ICON[o.status]} {a['title']}{tag}", f"ob:{a['id']}")])
        await self.say(chat, "📚 تکالیف تو (تازه‌ترین بالا):", inline(rows))

    async def cb_ob(self, chat, mid, arg):
        st = self.repo.student_by_chat(chat)
        a = self.repo.assignment(int(arg))
        if not st or not a or a["id"] not in {x["id"] for x in self.repo.assignments_for(st["id"])}:
            return
        now = utcnow()
        o, _, dl = self.repo.evaluate(a, st["id"], now)
        lines = [f"📌 {a['title']}", f"🏷 {KIND_FA[a['kind']]}" + (f" — {fa(a['bonus_points'])} امتیاز" if a["kind"] == "bonus" else "")]
        if a["description"] and a["description"] != "-":
            lines.append(f"📝 {a['description']}")
        can = self.repo.can_submit(a, st["id"], now)
        lines += [f"⏰ مهلت: {self.fmt(dl)}", "", self.status_line(o, dl, now, can)]
        w = self.repo.resubmit_window(a["id"], st["id"])
        if w and now <= from_db(w["until"]):
            lines.append(f"🔁 مدیر خواسته دوباره بفرستی (تا {self.fmt(from_db(w['until']))})"
                         + (f": {w['note']}" if w["note"] and w["note"] != "-" else ""))
        fb = next((f["feedback"] for f in reversed(self.repo.final_attempts(a["id"], st["id"])) if f["feedback"]), None)
        if fb:
            lines.append(f"💬 بازخورد مدیر: {fb}")
        kb = None
        if can:
            label = "📤 ارسال مجدد (جایگزین)" if self.repo.final_attempts(a["id"], st["id"]) else "📤 ارسال پاسخ"
            kb = inline([[(label, f"sub:{a['id']}")]])
        await self.say(chat, "\n".join(lines), kb)
        for f in self.repo.assignment_files(a["id"]):
            try:
                if f["tg_kind"] == "photo":
                    await self.bale.send_photo(chat, f["file_id"])
                else:
                    await self.bale.send_document(chat, f["file_id"], f["name"])
            except BaleError as e:
                log.warning("send assignment file failed: %s", e)

    async def cb_sub(self, chat, mid, arg):
        st = self.repo.student_by_chat(chat)
        a = self.repo.assignment(int(arg))
        if not st or not a:
            return
        if not self.repo.can_submit(a, st["id"], utcnow()):
            return await self.say(chat, "ارسال برای این تکلیف بسته شده. اگر لازم است از مدیر «ارسال مجدد» بخواه.")
        sub = self.repo.get_or_create_draft(a["id"], st["id"])
        n = len(self.repo.sub_files(sub))
        self.repo.set_session(chat, "submit", {"aid": a["id"], "sid": st["id"], "sub": sub})
        await self.say(chat, f"📤 پاسخ «{a['title']}» را بفرست.\nعکس یا فایل PDF؛ چند فایل هم می‌شود. "
                             f"وقتی تمام شد «✅ ثبت نهایی» را بزن." + (f"\n(الان {fa(n)} فایل دریافت شده)" if n else ""))

    async def st_submit(self, chat, data, m, text):
        f = extract_file(m)
        if not f:
            await self.say(chat, "فقط عکس یا PDF قبول می‌کنم 🙂 فایل را بفرست یا /cancel بزن.")
            return True
        if not f["ok"]:
            await self.say(chat, "این نوع فایل پشتیبانی نمی‌شود. فقط عکس یا PDF بفرست.")
            return True
        sub = data["sub"]
        dest = self.cfg.files_dir / f"a{data['aid']}" / f"s{data['sid']}_{uuid.uuid4().hex[:8]}_{f['name']}"
        ok = await self.bale.download(f["file_id"], dest)
        self.repo.add_sub_file(sub, f["kind"], f["file_id"], str(dest) if ok else None, f["name"], msg_time(m))
        n = len(self.repo.sub_files(sub))
        await self.say(chat, f"✅ فایل {fa(n)} دریافت شد. فایل دیگری هست؟ بفرست؛ وگرنه ثبت نهایی را بزن.",
                       inline([[(f"✅ ثبت نهایی ({fa(n)} فایل)", "fin:1")], [("🗑 لغو و پاک کردن", "cx:1")]]))
        return True

    async def cb_fin(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if state != "submit":
            return await self.say(chat, "ارسالی در جریان نیست. از «📚 تکالیف من» شروع کن.")
        a = self.repo.assignment(data["aid"])
        at = self.repo.finalize(data["sub"])
        if at is None:
            return await self.say(chat, "هنوز فایلی نفرستاده‌ای!")
        self.repo.clear_session(chat)
        o, _, dl = self.repo.evaluate(a, data["sid"], utcnow())
        attempts = len(self.repo.final_attempts(a["id"], data["sid"]))
        lines = [f"🎉 پاسخ «{a['title']}» ثبت شد."]
        if attempts > 1:
            lines.append("این ارسال به‌عنوان نسخهٔ جدید ذخیره شد.")
        lines.append(self.status_line(o, dl, utcnow()))
        new = self.repo.award(data["sid"], self.repo.summary(data["sid"], utcnow()).badges)
        if new:
            lines.append("\n🎖 نشان تازه گرفتی: " + "، ".join(new))
        await self.say(chat, "\n".join(lines), self.menu(chat))

    async def cb_cx(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if state == "submit":
            self.repo.discard_draft(data["sub"])
            self.repo.clear_session(chat)
        await self.say(chat, "ارسال لغو شد.", self.menu(chat))

    async def student_badges(self, chat, st):
        s = self.repo.summary(st["id"], utcnow())
        self.repo.award(st["id"], s.badges)
        got = self.repo.student_badges(st["id"])
        lines = ["🏅 نشان‌ها و امتیاز تو", ""]
        lines += [f"• {b['title']}" for b in got] or ["هنوز نشانی نگرفته‌ای؛ اولین تکلیف را به‌موقع بفرست 🌱"]
        lines += ["", f"🔥 مسیر نشان تعهد بعدی: {fa(s.run)} از {fa(STREAK_LEN)}", f"⭐ امتیاز: {fa(s.points)}",
                  f"✅ به‌موقع: {fa(s.on_time)}   ⚠️ با تاخیر: {fa(s.late)}   ❌ ارسال‌نشده: {fa(s.missing)}"]
        await self.say(chat, "\n".join(lines), self.menu(chat))

    # ------------------------------------------------------------ admin: groups & students
    async def admin_groups(self, chat):
        gs = self.repo.groups()
        text = "🗂 گروه‌ها:\n" + ("\n".join(f"• {g['name']} ({fa(g['n'])} نفر)" for g in gs) if gs else "هنوز گروهی نیست.")
        await self.say(chat, text, inline([[("➕ گروه جدید", "ag:new")]]))

    async def cb_ag(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        self.repo.set_session(chat, "ag_name", {})
        await self.say(chat, "نام گروه جدید را بنویس (مثلاً «ششم الف» یا «تقویتی ریاضی»):")

    async def st_ag_name(self, chat, data, m, text):
        if not text:
            return True
        self.repo.add_group(text)
        self.repo.clear_session(chat)
        await self.say(chat, f"✅ گروه «{text}» ساخته شد.", self.menu(chat))
        return True

    async def admin_students(self, chat):
        ss = self.repo.students()
        lines = ["👥 دانش‌آموزان:"]
        for s in ss:
            lines.append(f"• {s['full_name']} [{s['groups'] or 'بدون گروه'}] — کد {fa(s['code'])} "
                         f"{'✅ متصل' if s['chat_id'] else '⏳ منتظر ثبت‌نام'}")
        if not ss:
            lines.append("هنوز کسی نیست.")
        await self.say_long(chat, "\n".join(lines), inline([[("➕ افزودن یک نفر", "sa:one")], [("📥 افزودن گروهی", "sa:bulk")]]))

    async def cb_sa(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        if not self.repo.groups():
            return await self.say(chat, "اول از «🗂 گروه‌ها» یک گروه بساز.")
        if arg == "bulk":
            self.repo.set_session(chat, "bulk", {})
            return await self.say(chat, "هر خط یک دانش‌آموز، به این شکل:\nنام و نام‌خانوادگی | گروه۱، گروه۲\n"
                                        "(گروهِ ناموجود خودکار ساخته می‌شود)")
        self.repo.set_session(chat, "as_name", {})
        await self.say(chat, "نام و نام‌خانوادگی دانش‌آموز را بنویس:")

    def _group_toggle_kb(self, selected, prefix, extra_top=None, done_label="➡️ ادامه"):
        rows = list(extra_top or [])
        for g in self.repo.groups():
            mark = "✅ " if g["id"] in selected else "▫️ "
            rows.append([(mark + g["name"], f"{prefix}:{g['id']}")])
        rows.append([(done_label, f"{prefix}:done")])
        return inline(rows)

    async def st_as_name(self, chat, data, m, text):
        if not text:
            return True
        self.repo.set_session(chat, "as_groups", {"name": text, "groups": []})
        await self.say(chat, f"گروه(های) «{text}» را انتخاب کن:", self._group_toggle_kb([], "sg"))
        return True

    async def cb_sg(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "as_groups":
            return
        if arg == "done":
            if not data["groups"]:
                return await self.say(chat, "حداقل یک گروه انتخاب کن.")
            _, code = self.repo.add_student(data["name"], data["groups"])
            self.repo.clear_session(chat)
            return await self.say(
                chat, f"✅ {data['name']} ثبت شد.\n🔑 کد ورود: {fa(code)}\n"
                      f"این کد را به خود دانش‌آموز بده؛ او با /start وارد می‌شود.", self.menu(chat))
        gid = int(arg)
        sel = set(data["groups"]) ^ {gid}
        data["groups"] = sorted(sel)
        self.repo.set_session(chat, "as_groups", data)
        await self.edit_or_say(chat, mid, f"گروه(های) «{data['name']}» را انتخاب کن:", self._group_toggle_kb(sel, "sg"))

    async def st_bulk(self, chat, data, m, text):
        out, bad = [], []
        for line in text.splitlines():
            if not line.strip():
                continue
            name, sep, gtxt = line.partition("|")
            names = [g.strip() for g in gtxt.replace(",", "،").split("،") if g.strip()]
            if not name.strip() or not sep or not names:
                bad.append(line.strip())
                continue
            gids = [self.repo.add_group(g) for g in names]
            _, code = self.repo.add_student(name.strip(), gids)
            out.append(f"• {name.strip()} — کد {fa(code)}")
        self.repo.clear_session(chat)
        msg = f"✅ {fa(len(out))} دانش‌آموز ثبت شد:\n" + "\n".join(out)
        if bad:
            msg += "\n\n⚠️ این خط‌ها فرمت درست نداشتند:\n" + "\n".join(bad)
        await self.say_long(chat, msg, self.menu(chat))
        return True

    # ------------------------------------------------------------ admin: new assignment wizard
    async def admin_new_assignment(self, chat):
        if not self.repo.groups():
            await self.say(chat, "ℹ️ هنوز گروهی نساخته‌ای؛ دانش‌آموزان بدون گروه نمی‌توانند ثبت‌نام کنند.")
        self.repo.set_session(chat, "na_title", {"files": [], "groups": [], "all": False})
        await self.say(chat, "➕ تکلیف جدید\n۱) عنوان تکلیف را بنویس (برای انصراف /cancel):")

    async def st_na_title(self, chat, data, m, text):
        if not text:
            return True
        data["title"] = text
        self.repo.set_session(chat, "na_desc", data)
        await self.say(chat, "۲) توضیح تکلیف را بنویس (اگر توضیحی نداری فقط یک «-» بفرست):")
        return True

    async def st_na_desc(self, chat, data, m, text):
        if not text:
            return True
        data["desc"] = text
        self.repo.set_session(chat, "na_files", data)
        await self.say(chat, "۳) عکس یا PDF تکلیف را بفرست (چندتا هم می‌شود). اگر فایلی نداری «ادامه» را بزن.",
                       inline([[("➡️ ادامه", "na_files:go")]]))
        return True

    async def st_na_files(self, chat, data, m, text):
        f = extract_file(m)
        if not f or not f["ok"]:
            await self.say(chat, "عکس یا PDF بفرست، یا «ادامه» را بزن.", inline([[("➡️ ادامه", "na_files:go")]]))
            return True
        data["files"].append({k: f[k] for k in ("kind", "file_id", "name")})
        self.repo.set_session(chat, "na_files", data)
        await self.say(chat, f"✅ فایل {fa(len(data['files']))} دریافت شد. فایل بعدی یا «ادامه»:",
                       inline([[("➡️ ادامه", "na_files:go")]]))
        return True

    async def cb_na_files(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "na_files":
            return
        self.repo.set_session(chat, "na_kind", data)
        await self.say(chat, "۴) نوع تکلیف:", inline([
            [("📘 تکلیف اصلی (در نشان تعهد حساب می‌شود)", "na_k:regular")],
            [("🌿 اختیاری (فقط تشویقی)", "na_k:optional")],
            [("⭐ امتیازی (با امتیاز)", "na_k:bonus")]]))

    async def cb_na_k(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "na_kind" or arg not in KIND_FA:
            return
        data["kind"], data["points"] = arg, 0
        if arg == "bonus":
            self.repo.set_session(chat, "na_pts", data)
            return await self.say(chat, "چند امتیاز؟ (یک عدد بفرست، مثلاً ۵)")
        await self._ask_targets(chat, data)

    async def st_na_pts(self, chat, data, m, text):
        t = en(text).strip()
        if not t.isdigit():
            await self.say(chat, "فقط یک عدد بفرست، مثلاً ۵")
            return True
        data["points"] = int(t)
        await self._ask_targets(chat, data)
        return True

    async def _ask_targets(self, chat, data):
        self.repo.set_session(chat, "na_target", data)
        await self.say(chat, "۵) این تکلیف برای چه کسانی است؟", self._target_kb(data))

    def _target_kb(self, data, prefix="na_t"):
        top = [[(("✅ " if data["all"] else "▫️ ") + "🌐 همهٔ دانش‌آموزان", f"{prefix}:all")]]
        return self._group_toggle_kb(set(data["groups"]), prefix, top)

    async def cb_na_t(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "na_target":
            return
        if arg == "done":
            if not data["all"] and not data["groups"]:
                return await self.say(chat, "حداقل یک گزینه انتخاب کن.")
            tomorrow = (utcnow() + timedelta(days=1)).astimezone(self.cfg.tz)
            self.repo.set_session(chat, "na_deadline", data)
            return await self.say(
                chat, "۶) مهلت ارسال را بنویس. مثال‌ها:\n"
                      f"• {jalali_str(tomorrow)} {fa('21:00')}\n• فردا {fa('21:00')}\n• امروز {fa('23:30')}")
        if arg == "all":
            data["all"], data["groups"] = not data["all"], []
        else:
            data["all"] = False
            data["groups"] = sorted(set(data["groups"]) ^ {int(arg)})
        self.repo.set_session(chat, "na_target", data)
        await self.edit_or_say(chat, mid, "۵) این تکلیف برای چه کسانی است؟", self._target_kb(data))

    async def st_na_deadline(self, chat, data, m, text):
        try:
            dl = parse_deadline(text, self.cfg.tz, utcnow())
        except ValueError:
            await self.say(chat, f"متوجه تاریخ/ساعت نشدم 🤔 به این شکل بنویس:\nفردا {fa('21:00')}  یا  "
                                 f"{jalali_str(utcnow().astimezone(self.cfg.tz))} {fa('21:00')}")
            return True
        if dl <= utcnow():
            await self.say(chat, "این زمان گذشته است؛ زمان آینده بنویس.")
            return True
        data["deadline"] = to_db(dl)
        self.repo.set_session(chat, "na_confirm", data)
        names = "همه" if data["all"] else "، ".join(g["name"] for g in self.repo.groups() if g["id"] in data["groups"])
        summary = (f"📋 خلاصهٔ تکلیف\n📌 {data['title']}\n🏷 {KIND_FA[data['kind']]}"
                   + (f" ({fa(data['points'])} امتیاز)" if data["kind"] == "bonus" else "")
                   + f"\n📝 {data['desc']}\n📎 فایل: {fa(len(data['files']))}\n👥 برای: {names}"
                   f"\n⏰ مهلت: {self.fmt(dl)}\n⏱ مهلت ارفاقی: {fa(self.cfg.grace_minutes)} دقیقه")
        await self.say(chat, summary, inline([[("✅ انتشار و اطلاع‌رسانی", "na_ok:yes"), ("❌ لغو", "na_ok:no")]]))
        return True

    async def cb_na_ok(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "na_confirm":
            return
        self.repo.clear_session(chat)
        if arg != "yes":
            return await self.say(chat, "لغو شد.", self.menu(chat))
        targets = [("all", 0)] if data["all"] else [("group", g) for g in data["groups"]]
        aid = self.repo.create_assignment(data["title"], data["desc"], data["kind"], data["deadline"],
                                          self.cfg.grace_minutes, data["points"], targets, data["files"])
        studs = self.repo.students_for(aid)
        sent = 0
        for s in studs:
            if not s["chat_id"]:
                continue
            try:
                await self.say(s["chat_id"], f"📣 تکلیف جدید: {data['title']}\n⏰ مهلت: {self.fmt(from_db(data['deadline']))}",
                               inline([[("👀 مشاهده و ارسال", f"ob:{aid}")]]))
                sent += 1
            except BaleError as e:
                log.warning("notify failed for %s: %s", s["id"], e)
            await asyncio.sleep(0.05)
        waiting = sum(1 for s in studs if not s["chat_id"])
        await self.say(chat, f"✅ منتشر شد و برای {fa(sent)} نفر ارسال شد."
                             + (f"\n(⏳ {fa(waiting)} نفر هنوز ثبت‌نام نکرده‌اند)" if waiting else ""), self.menu(chat))

    # ------------------------------------------------------------ admin: report
    async def admin_assignments(self, chat):
        items = self.repo.active_assignments()[:25]
        if not items:
            return await self.say(chat, "هنوز تکلیفی نساخته‌ای.", self.menu(chat))
        now = utcnow()
        rows = []
        for a in items:
            studs = [s for s in self.repo.students_for(a["id"]) if self.repo.counts_for(a, s)]
            done = sum(1 for s in studs if self.repo.evaluate(a, s["id"], now)[1] is not None)
            rows.append([(f"{a['title']} — {fa(done)}/{fa(len(studs))}", f"rep:{a['id']}")])
        await self.say(chat, "📋 تکالیف (برای گزارش کامل یکی را بزن):", inline(rows))

    async def cb_rep(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        a = self.repo.assignment(int(arg))
        if not a:
            return
        now = utcnow()
        buckets = {ON_TIME: [], LATE: [], OPEN: [], MISSING: [], EXCUSED: []}
        unreg = []
        for s in self.repo.students_for(a["id"]):
            if not self.repo.counts_for(a, s):
                continue
            o, _, _ = self.repo.evaluate(a, s["id"], now)
            label = s["full_name"]
            if o.status == LATE:
                label += f" ({fmt_duration(o.late_seconds)})"
            elif o.status == ON_TIME and o.in_grace:
                label += " (مهلت ارفاقی)"
            buckets[o.status].append(label)
            if not s["chat_id"]:
                unreg.append(s["full_name"])
        title = {ON_TIME: "✅ به‌موقع", LATE: "⚠️ با تاخیر", OPEN: "⏳ هنوز در مهلت", MISSING: "❌ ارسال‌نشده (مهلت گذشته)",
                 EXCUSED: "🎓 عذر موجه"}
        lines = [f"📊 {a['title']} ({KIND_FA[a['kind']]})", f"⏰ مهلت: {self.fmt(from_db(a['deadline']))}", ""]
        for k, t in title.items():
            if buckets[k]:
                lines.append(f"{t} ({fa(len(buckets[k]))}): " + "، ".join(buckets[k]))
        if unreg:
            lines.append(f"\n🔔 هنوز در ربات ثبت‌نام نکرده‌اند: " + "، ".join(unreg))
        aid = a["id"]
        kb = inline([[("👤 بررسی دانش‌آموز", f"pl:{aid}"), ("📥 دانلود پاسخ‌ها", f"dz:{aid}")],
                     [("⏰ تمدید برای همه", f"ea:{aid}"), ("✏️ ویرایش", f"et:{aid}")],
                     [("🗄 بایگانی", f"ar:{aid}")]])
        await self.say_long(chat, "\n".join(lines), kb)

    # ============================================================ فاز ۲
    async def send_logo(self, chat):
        try:
            fid = self.repo.meta_get("logo_file_id")
            if fid:
                await self.bale.send_photo(chat, fid)
                return
            r = await self.bale.send_file(chat, LOGO, "photo")
            self.repo.meta_set("logo_file_id", r["photo"][-1]["file_id"])
        except (BaleError, KeyError, IndexError, TypeError) as e:
            log.info("logo skipped: %s", e)

    async def notify(self, sid, text, kb=None):
        s = self.repo.student(sid)
        if s and s["chat_id"]:
            try:
                await self.say(s["chat_id"], text, kb)
            except BaleError as e:
                log.warning("notify %s failed: %s", sid, e)

    def admin_status(self, o):
        base = {ON_TIME: "✅ به‌موقع", LATE: f"⚠️ با {fmt_duration(o.late_seconds)} تاخیر", MISSING: "❌ ارسال‌نشده",
                OPEN: "⏳ هنوز در مهلت", EXCUSED: "🎓 عذر موجه"}[o.status]
        return base + (" (مهلت ارفاقی)" if o.in_grace else "")

    # ---------------- بررسی تک‌تک دانش‌آموزان
    async def cb_pl(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        a = self.repo.assignment(int(arg))
        now = utcnow()
        rows = []
        for s in self.repo.students_for(a["id"]):
            if self.repo.counts_for(a, s):
                o, _, _ = self.repo.evaluate(a, s["id"], now)
                rows.append([(f"{ICON[o.status]} {s['full_name']}", f"ps:{a['id']}:{s['id']}")])
        await self.say(chat, f"👤 دانش‌آموزان «{a['title']}»:", inline(rows))

    async def cb_ps(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        a, s, now = self.repo.assignment(aid), self.repo.student(sid), utcnow()
        o, sub_at, dl = self.repo.evaluate(a, sid, now)
        finals = self.repo.final_attempts(aid, sid)
        ov = self.repo.one("SELECT 1 FROM deadline_overrides WHERE assignment_id=? AND student_id=?", (aid, sid))
        lines = [f"👤 {s['full_name']}", f"📌 {a['title']}",
                 f"⏰ مهلت او: {self.fmt(dl)}" + (" (تمدید فردی)" if ov else ""), f"وضعیت: {self.admin_status(o)}",
                 f"📎 تعداد ارسال: {fa(len(finals))}" + (f" — آخرین: {self.fmt(from_db(finals[-1]['submitted_at']))}" if finals else "")]
        fb = next((f["feedback"] for f in reversed(finals) if f["feedback"]), None)
        if fb:
            lines.append(f"💬 بازخورد فعلی: {fb}")
        w = self.repo.resubmit_window(aid, sid)
        if w and now <= from_db(w["until"]):
            lines.append(f"🔁 پنجرهٔ ارسال مجدد باز است تا {self.fmt(from_db(w['until']))}")
        ex = self.repo.is_excused(aid, sid)
        kb = inline([[("📎 دیدن پاسخ", f"pf:{arg}"), ("💬 بازخورد", f"pb:{arg}")],
                     [("🔁 درخواست ارسال مجدد", f"pr:{arg}"), ("⏰ تمدید فردی", f"pe:{arg}")],
                     [(("↩️ لغو عذر موجه" if ex else "🎓 ثبت عذر موجه"), f"px:{arg}")]])
        await self.say(chat, "\n".join(lines), kb)

    async def cb_pf(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        last = self.repo.latest_final(aid, sid)
        if not last:
            return await self.say(chat, "این دانش‌آموز هنوز پاسخی نفرستاده.")
        s = self.repo.student(sid)
        await self.say(chat, f"📎 پاسخ {s['full_name']} (نسخهٔ {fa(last['attempt_no'])}، {self.fmt(from_db(last['submitted_at']))}):")
        for f in self.repo.sub_files(last["id"]):
            try:
                if f["tg_kind"] == "photo":
                    await self.bale.send_photo(chat, f["file_id"])
                else:
                    await self.bale.send_document(chat, f["file_id"], f["name"])
            except BaleError as e:
                log.warning("send submission file failed: %s", e)

    async def cb_pb(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        if not self.repo.latest_final(aid, sid):
            return await self.say(chat, "هنوز پاسخی ثبت نشده که بازخورد بگیرد.")
        self.repo.set_session(chat, "fb", {"aid": aid, "sid": sid})
        await self.say(chat, "💬 متن بازخورد را بنویس (مستقیم برای دانش‌آموز می‌رود):")

    async def st_fb(self, chat, data, m, text):
        if not text:
            return True
        last = self.repo.latest_final(data["aid"], data["sid"])
        a = self.repo.assignment(data["aid"])
        self.repo.set_feedback(last["id"], text)
        self.repo.clear_session(chat)
        await self.notify(data["sid"], f"💬 بازخورد مدیر برای «{a['title']}»:\n{text}", inline([[("👀 مشاهده", f"ob:{a['id']}")]]))
        await self.say(chat, "✅ بازخورد ثبت و برای دانش‌آموز ارسال شد.", self.menu(chat))
        return True

    async def cb_pr(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        if not self.repo.latest_final(aid, sid):
            return await self.say(chat, "این دانش‌آموز هنوز پاسخی نفرستاده؛ برای او «⏰ تمدید فردی» را بزن.")
        self.repo.set_session(chat, "rq_note", {"aid": aid, "sid": sid})
        await self.say(chat, "🔁 دلیل یا راهنما برای ارسال مجدد را بنویس (مثلاً «عکس تار بود»). اگر توضیحی نداری «-» بفرست:")

    async def st_rq_note(self, chat, data, m, text):
        if not text:
            return True
        a = self.repo.assignment(data["aid"])
        until = self.repo.request_resubmit(data["aid"], data["sid"], text)
        self.repo.clear_session(chat)
        await self.notify(data["sid"], f"🔁 مدیر خواسته «{a['title']}» را دوباره بفرستی."
                          + (f"\n📝 {text}" if text != "-" else "") + f"\n⏳ فرصت ارسال تا {self.fmt(until)}",
                          inline([[("📤 مشاهده و ارسال", f"ob:{a['id']}")]]))
        await self.say(chat, f"✅ درخواست ارسال شد؛ پنجرهٔ ارسال مجدد تا {self.fmt(until)} باز است.", self.menu(chat))
        return True

    async def cb_pe(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        self.repo.set_session(chat, "ex_dl", {"aid": aid, "sid": sid})
        await self.say(chat, f"⏰ مهلت جدید برای این دانش‌آموز را بنویس (مثلاً فردا {fa('21:00')}):")

    async def st_ex_dl(self, chat, data, m, text):
        dl = await self._parse_dl(chat, text)
        if not dl:
            return True
        a = self.repo.assignment(data["aid"])
        self.repo.set_override(data["aid"], data["sid"], to_db(dl))
        self.repo.clear_session(chat)
        await self.notify(data["sid"], f"⏰ مهلت «{a['title']}» برای تو تمدید شد تا {self.fmt(dl)}",
                          inline([[("📤 مشاهده و ارسال", f"ob:{a['id']}")]]))
        await self.say(chat, "✅ مهلت فردی ثبت شد و به دانش‌آموز اطلاع داده شد.", self.menu(chat))
        return True

    async def cb_px(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        aid, sid = (int(x) for x in arg.split(":"))
        a = self.repo.assignment(aid)
        on = self.repo.toggle_excuse(aid, sid)
        await self.notify(sid, f"🎓 برای «{a['title']}» عذر تو پذیرفته شد." if on else f"ℹ️ عذر موجه «{a['title']}» لغو شد.")
        await self.say(chat, "✅ عذر موجه ثبت شد (نه تاخیر حساب می‌شود، نه شمارش نشان را می‌شکند)." if on else "↩️ عذر موجه لغو شد.")
        await self.cb_ps(chat, mid, arg)

    async def _parse_dl(self, chat, text):
        try:
            dl = parse_deadline(text, self.cfg.tz, utcnow())
        except ValueError:
            await self.say(chat, f"متوجه تاریخ/ساعت نشدم 🤔 مثل این بنویس: فردا {fa('21:00')}")
            return None
        if dl <= utcnow():
            await self.say(chat, "این زمان گذشته است؛ زمان آینده بنویس.")
            return None
        return dl

    # ---------------- تمدید برای همه / ویرایش / بایگانی
    async def cb_ea(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        self.repo.set_session(chat, "ea_dl", {"aid": int(arg)})
        await self.say(chat, f"⏰ مهلت جدید (برای همه) را بنویس، مثلاً فردا {fa('21:00')}:")

    async def st_ea_dl(self, chat, data, m, text):
        dl = await self._parse_dl(chat, text)
        if not dl:
            return True
        a = self.repo.assignment(data["aid"])
        self.repo.update_assignment(a["id"], deadline=to_db(dl))
        self.repo.clear_session(chat)
        sent = 0
        for s in self.repo.students_for(a["id"]):
            if s["chat_id"]:
                await self.notify(s["id"], f"⏰ مهلت «{a['title']}» تمدید شد تا {self.fmt(dl)}", inline([[("👀 مشاهده", f"ob:{a['id']}")]]))
                sent += 1
                await asyncio.sleep(0.05)
        await self.say(chat, f"✅ مهلت تمدید شد و به {fa(sent)} نفر اطلاع داده شد. (تمدیدهای فردیِ قبلی سر جایشان می‌مانند)", self.menu(chat))
        return True

    async def cb_et(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        await self.say(chat, "چه چیزی را ویرایش کنم؟", inline([[("✏️ عنوان", f"ed:t:{arg}"), ("✏️ توضیح", f"ed:d:{arg}")]]))

    async def cb_ed(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        field, _, aid = arg.partition(":")
        self.repo.set_session(chat, "ed_" + field, {"aid": int(aid)})
        await self.say(chat, "متن جدید را بنویس:")

    async def st_ed_t(self, chat, data, m, text):
        if text:
            self.repo.update_assignment(data["aid"], title=text)
            self.repo.clear_session(chat)
            await self.say(chat, "✅ عنوان تغییر کرد.", self.menu(chat))
        return True

    async def st_ed_d(self, chat, data, m, text):
        if text:
            self.repo.update_assignment(data["aid"], description=text)
            self.repo.clear_session(chat)
            await self.say(chat, "✅ توضیح تغییر کرد.", self.menu(chat))
        return True

    async def cb_ar(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        self.repo.update_assignment(int(arg), status="archived")
        await self.say(chat, "🗄 بایگانی شد: از فهرست دانش‌آموز و مدیر پنهان می‌شود، ولی در آمار و نشان‌ها همچنان حساب می‌شود.")

    # ---------------- دانلود دسته‌ای و اکسل
    async def cb_dz(self, chat, mid, arg):
        if not self.is_admin(chat):
            return
        a = self.repo.assignment(int(arg))
        await self.say(chat, "⏳ در حال آماده‌سازی فایل zip ...")
        out = self.cfg.data_dir / "exports"
        out.mkdir(parents=True, exist_ok=True)
        path, n_files, n_st, missing = await build_zip(self.repo, self.bale, self.cfg, a["id"], out / f"answers_{a['id']}.zip")
        if not n_files:
            return await self.say(chat, "هنوز پاسخی برای دانلود نیست.")
        try:
            await self.bale.send_file(chat, path, "document", f"{a['title']} — {fa(n_files)} فایل از {fa(n_st)} دانش‌آموز")
        except BaleError as e:
            log.warning("zip send failed: %s", e)
            await self.say(chat, f"⚠️ ارسال فایل zip ناموفق بود (شاید حجم زیاد است). فایل روی سرور ذخیره شد: {path}")
        if missing:
            await self.say(chat, f"⚠️ {fa(missing)} فایل دانلود نشد و در zip نیست.")

    async def admin_excel(self, chat):
        out = self.cfg.data_dir / "exports"
        out.mkdir(parents=True, exist_ok=True)
        path = build_excel(self.repo, self.cfg, out / "assignment_stats.xlsx", utcnow())
        try:
            await self.bale.send_file(chat, path, "document", "📊 آمار تکالیف")
        except BaleError as e:
            log.warning("excel send failed: %s", e)
            await self.say(chat, f"⚠️ ارسال فایل ناموفق بود. فایل روی سرور ذخیره شد: {path}")

    # ---------------- پیام همگانی
    async def admin_broadcast(self, chat):
        data = {"all": False, "groups": []}
        self.repo.set_session(chat, "bc_target", data)
        await self.say(chat, "📢 پیام همگانی برای چه کسانی؟", self._target_kb(data, "bc"))

    async def cb_bc(self, chat, mid, arg):
        state, data = self.repo.get_session(chat)
        if not self.is_admin(chat) or state != "bc_target":
            return
        if arg == "done":
            if not data["all"] and not data["groups"]:
                return await self.say(chat, "حداقل یک گزینه انتخاب کن.")
            self.repo.set_session(chat, "bc_text", data)
            return await self.say(chat, "متن پیام را بنویس:")
        if arg == "all":
            data["all"], data["groups"] = not data["all"], []
        else:
            data["all"] = False
            data["groups"] = sorted(set(data["groups"]) ^ {int(arg)})
        self.repo.set_session(chat, "bc_target", data)
        await self.edit_or_say(chat, mid, "📢 پیام همگانی برای چه کسانی؟", self._target_kb(data, "bc"))

    async def st_bc_text(self, chat, data, m, text):
        if not text:
            return True
        studs = self.repo.registered_students(None if data["all"] else data["groups"])
        self.repo.clear_session(chat)
        for s in studs:
            await self.notify(s["id"], f"📢 پیام مجموعه رشد:\n{text}")
            await asyncio.sleep(0.05)
        await self.say(chat, f"✅ برای {fa(len(studs))} نفر ارسال شد.", self.menu(chat))
        return True
