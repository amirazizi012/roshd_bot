"""ابزارهای زمان: ارقام فارسی، تاریخ شمسی، تجزیهٔ مهلت، قالب‌بندی مدت."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import jdatetime

FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_TO_FA = str.maketrans("0123456789", FA_DIGITS)
_TO_EN = str.maketrans(FA_DIGITS + "٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)

# Python: Monday=0
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def fa(x) -> str:
    """همهٔ ارقام را فارسی می‌کند."""
    return str(x).translate(_TO_FA)


def en(s: str) -> str:
    """ارقام فارسی/عربی را به لاتین برمی‌گرداند (برای تجزیه)."""
    return s.translate(_TO_EN)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def from_db(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def jalali_str(local: datetime) -> str:
    jd = jdatetime.date.fromgregorian(date=local.date())
    return fa(f"{jd.year:04d}/{jd.month:02d}/{jd.day:02d}")


def fmt_dt(dt: datetime, tz) -> str:
    local = dt.astimezone(tz)
    return fa(f"{WEEKDAYS[local.weekday()]} {jalali_str(local)} - ساعت {local:%H:%M}")


def fmt_duration(seconds: float) -> str:
    s = int(abs(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if not d and not h and m == 0:
        return "کمتر از یک دقیقه"
    parts = []
    if d:
        parts.append(f"{fa(d)} روز")
    if h:
        parts.append(f"{fa(h)} ساعت")
    if m:
        parts.append(f"{fa(m)} دقیقه")
    return " و ".join(parts)


# «پس‌فردا» باید قبل از «فردا» بیاید چون شامل آن است
_REL = [("پس‌فردا", 2), ("پس فردا", 2), ("پسفردا", 2), ("فردا", 1), ("امروز", 0)]


def parse_deadline(text: str, tz, now: datetime) -> datetime:
    """مثال‌ها: «۱۴۰۵/۰۸/۲۰ ۲۱:۰۰»، «فردا ۲۱:۰۰»، «امروز ۲۳:۳۰». خروجی UTC."""
    t = en(text).replace("٫", ":").replace("،", " ").strip()
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", t)
    if not m:
        raise ValueError("ساعت")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        raise ValueError("ساعت")
    rest = (t[: m.start()] + " " + t[m.end():]).strip()
    local_now = now.astimezone(tz)
    day = None
    for word, off in _REL:
        if word in rest:
            day = (local_now + timedelta(days=off)).date()
            break
    if day is None:
        d = re.search(r"(\d{4})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{1,2})", rest)
        if not d:
            raise ValueError("تاریخ")
        y, mo, da = (int(g) for g in d.groups())
        try:
            day = jdatetime.date(y, mo, da).togregorian()
        except ValueError:
            raise ValueError("تاریخ") from None
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    return local.astimezone(timezone.utc)
