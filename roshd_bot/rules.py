"""قوانین خالص (بدون دیتابیس و شبکه): وضعیت تحویل، دنبالهٔ به‌موقع، نشان‌ها."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .timeutil import fa

ON_TIME, LATE, MISSING, EXCUSED, OPEN = "on_time", "late", "missing", "excused", "open"

STREAK_LEN = 5          # هر ۵ تکلیف به‌موقعِ پشت‌سرهم = یک نشان تعهد + امتیاز
STREAK_POINTS = 10      # امتیازِ هر نشان تعهد
EARLY_HOURS = 12        # ارسال حداقل ۱۲ ساعت زودتر از مهلت = «زودهنگام»
EARLY_NEEDED = 5
EXPLORER_NEEDED = 3
COMEBACK_NEEDED = 3


@dataclass(frozen=True)
class Outcome:
    status: str
    late_seconds: int = 0
    in_grace: bool = False


def classify(submitted_at, deadline: datetime, grace_min: int, now: datetime, excused: bool = False) -> Outcome:
    """
    - تا deadline+grace به‌موقع حساب می‌شود (اگر بعد از deadline باشد in_grace=True).
    - تاخیر از خود deadline (نه پایان مهلت ارفاقی) اندازه‌گیری می‌شود.
    - «عذر موجه» تاخیر یا ارسال‌نشدن را خنثی می‌کند (EXCUSED).
    """
    limit = deadline + timedelta(minutes=grace_min)
    if submitted_at is not None:
        if submitted_at <= limit:
            return Outcome(ON_TIME, in_grace=submitted_at > deadline)
        if excused:
            return Outcome(EXCUSED)
        return Outcome(LATE, late_seconds=int((submitted_at - deadline).total_seconds()))
    if excused:
        return Outcome(EXCUSED)
    return Outcome(OPEN) if now <= limit else Outcome(MISSING)


@dataclass(frozen=True)
class Record:
    kind: str                      # regular | optional | bonus
    deadline: datetime
    status: str
    submitted_at: datetime | None = None
    bonus_points: int = 0


@dataclass
class Summary:
    badges: dict = field(default_factory=dict)   # key -> عنوان
    run: int = 0                                  # پیشرفت فعلی تا نشان بعدی
    points: int = 0
    on_time: int = 0
    late: int = 0
    missing: int = 0


def summarize(records: list) -> Summary:
    s = Summary()
    regular = sorted(
        (r for r in records if r.kind == "regular" and r.status in (ON_TIME, LATE, MISSING)),
        key=lambda r: r.deadline,
    )
    run = since_break = chains = 0
    had_break = False
    early = 0
    for r in regular:
        if r.status == ON_TIME:
            s.on_time += 1
            run += 1
            since_break += 1
            if r.submitted_at and r.submitted_at <= r.deadline - timedelta(hours=EARLY_HOURS):
                early += 1
            if run == STREAK_LEN:
                chains += 1
                s.badges[f"commit:{chains}"] = f"🏅 نشان تعهد {fa(chains)}"
                run = 0
            if had_break and since_break == COMEBACK_NEEDED:
                s.badges["comeback"] = "💪 بازگشت قهرمانانه"
        else:
            s.late += r.status == LATE
            s.missing += r.status == MISSING
            run = since_break = 0
            had_break = True
    s.run = run
    if early >= EARLY_NEEDED:
        s.badges["early"] = "🌅 پیشگام"
    if any(r.status == ON_TIME for r in records):
        s.badges["first_step"] = "🌱 اولین گام"
    extras = [r for r in records if r.kind in ("optional", "bonus") and r.submitted_at is not None]
    if len(extras) >= EXPLORER_NEEDED:
        s.badges["explorer"] = "🔭 کنجکاو"
    s.points = (sum(r.bonus_points for r in records if r.kind == "bonus" and r.status == ON_TIME)
                + chains * STREAK_POINTS)
    return s
