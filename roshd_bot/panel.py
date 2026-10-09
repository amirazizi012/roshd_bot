"""پنل وب «فقط خواندنی» با رمز ثابت. هیچ مسیر تغییردهنده‌ای (جز ورود/خروج) ندارد."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import tempfile
import time
from pathlib import Path

from aiohttp import web

from .export import LOGO, build_excel
from .rules import EXCUSED, LATE, MISSING, ON_TIME, OPEN, STREAK_LEN
from .timeutil import fa, fmt_dt, fmt_duration, from_db, jalali_str, utcnow

log = logging.getLogger("panel")
COOKIE = "roshd_panel"
SESSION_SECONDS = 7 * 86400
KIND_FA = {"regular": "اصلی", "optional": "اختیاری", "bonus": "امتیازی"}
LABEL = {ON_TIME: "به‌موقع", LATE: "با تاخیر", MISSING: "ارسال‌نشده", OPEN: "در مهلت", EXCUSED: "عذر موجه"}
esc = html.escape

CSS = """
*{box-sizing:border-box}body{font-family:Tahoma,'Segoe UI',sans-serif;background:#f4f8f4;color:#243;margin:0;direction:rtl}
header{background:#fff;border-bottom:3px solid #8bc888;padding:10px 20px;display:flex;align-items:center;gap:14px}
header img{height:46px}header h1{font-size:18px;margin:0;color:#2f6b3a;flex:1}
a{color:#2f6b3a;text-decoration:none}a:hover{text-decoration:underline}
main{max-width:1100px;margin:18px auto;padding:0 14px}
h2{color:#2f6b3a;font-size:17px;margin:22px 0 8px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:14px}
th{background:#4e9a5b;color:#fff;padding:8px}td{padding:7px 8px;border-bottom:1px solid #e3ebe3;text-align:center}
td.l{text-align:right}.wrap{overflow-x:auto}
.s-on_time{background:#d9f2d9}.s-late{background:#ffe7b3}.s-missing{background:#f8c9c9}.s-open{background:#e8eef7}.s-excused{background:#e6ddf3}
.btn{display:inline-block;background:#4e9a5b;color:#fff;padding:7px 14px;border-radius:6px}.btn:hover{text-decoration:none;opacity:.9}
.box{background:#fff;border-radius:8px;padding:14px;margin:10px 0}.muted{color:#789}
img.th{height:70px;border-radius:4px;margin:2px;border:1px solid #cdd}
form{max-width:340px;margin:60px auto;background:#fff;padding:24px;border-radius:10px;text-align:center}
input[type=password]{width:100%;padding:10px;margin:12px 0;border:1px solid #bcd;border-radius:6px;font-size:16px}
button{background:#4e9a5b;color:#fff;border:0;padding:10px 24px;border-radius:6px;font-size:15px;cursor:pointer}
.err{color:#b00}
"""


def page(title: str, body: str, org: str, logged_in: bool = True) -> web.Response:
    nav = '<a class="btn" href="/">خانه</a> <a href="/logout">خروج</a>' if logged_in else ""
    doc = (f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
           f"<style>{CSS}</style></head><body><header><img src='/logo.png' alt=''><h1>{esc(org)} — پنل تکالیف (فقط‌خواندنی)</h1>{nav}</header>"
           f"<main>{body}</main></body></html>")
    return web.Response(text=doc, content_type="text/html", charset="utf-8")


def make_app(cfg, repo) -> web.Application:
    secret = hashlib.sha256(f"{cfg.panel_password}|{cfg.token}|roshd".encode()).digest()
    fails: dict = {}

    def sign(exp: int) -> str:
        return f"{exp}.{hmac.new(secret, str(exp).encode(), hashlib.sha256).hexdigest()}"

    def valid(cookie) -> bool:
        try:
            exp, sig = (cookie or "").split(".", 1)
            return int(exp) > time.time() and hmac.compare_digest(sig, sign(int(exp)).split(".", 1)[1])
        except ValueError:
            return False

    @web.middleware
    async def guard(request, handler):
        if request.path in ("/login", "/logo.png") or valid(request.cookies.get(COOKIE)):
            return await handler(request)
        raise web.HTTPFound("/login")

    # ---------- ورود/خروج
    async def login_form(request, msg=""):
        body = ('<form method="post"><h2>ورود به پنل</h2>'
                f'<div class="err">{esc(msg)}</div><input type="password" name="password" placeholder="رمز" autofocus>'
                "<button>ورود</button></form>")
        return page("ورود", body, cfg.org_name, logged_in=False)

    async def login_get(request):
        return await login_form(request)

    async def login_post(request):
        ip = request.remote or "?"
        n, until = fails.get(ip, (0, 0))
        if until > time.time():
            return await login_form(request, "تلاش‌های ناموفق زیاد بود؛ چند دقیقه بعد دوباره امتحان کن.")
        pw = (await request.post()).get("password", "")
        if hmac.compare_digest(pw.encode(), cfg.panel_password.encode()):
            fails.pop(ip, None)
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, sign(int(time.time()) + SESSION_SECONDS), max_age=SESSION_SECONDS,
                            httponly=True, samesite="Lax")
            raise resp
        n += 1
        fails[ip] = (n, time.time() + 300 if n >= 5 else 0)
        return await login_form(request, "رمز درست نیست.")

    async def logout(request):
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE)
        raise resp

    async def logo(request):
        return web.FileResponse(LOGO)

    # ---------- صفحات
    def status_cell(o) -> str:
        t = LABEL[o.status] + (" (ارفاقی)" if o.in_grace else "")
        if o.status == LATE:
            t += f"<br><small>{fmt_duration(o.late_seconds)}</small>"
        return f'<td class="s-{o.status}">{t}</td>'

    async def dashboard(request):
        now = utcnow()
        rows = []
        for s in repo.students():
            sm = repo.summary(s["id"], now)
            denom = sm.on_time + sm.late + sm.missing
            pct = f"{fa(round(100 * sm.on_time / denom))}٪" if denom else "—"
            badges = "، ".join(b["title"] for b in repo.student_badges(s["id"])) or "—"
            rows.append(f"<tr><td class='l'><a href='/student/{s['id']}'>{esc(s['full_name'])}</a></td>"
                        f"<td>{esc(s['groups'] or '')}</td><td>{fa(sm.on_time)}</td><td>{fa(sm.late)}</td>"
                        f"<td>{fa(sm.missing)}</td><td>{pct}</td><td>{fa(sm.points)}</td><td class='l'>{esc(badges)}</td></tr>")
        arows = []
        for a in repo.q("SELECT * FROM assignments ORDER BY deadline DESC"):
            studs = [s for s in repo.students_for(a["id"]) if repo.counts_for(a, s)]
            done = sum(1 for s in studs if repo.evaluate(a, s["id"], now)[1] is not None)
            arows.append(f"<tr><td class='l'><a href='/assignment/{a['id']}'>{esc(a['title'])}</a>"
                         f"{' 🗄' if a['status'] == 'archived' else ''}</td><td>{KIND_FA[a['kind']]}</td>"
                         f"<td>{fmt_dt(from_db(a['deadline']), cfg.tz)}</td><td>{fa(done)} / {fa(len(studs))}</td></tr>")
        body = (f"<p><a class='btn' href='/export.xlsx'>📊 دانلود اکسل آمار</a></p><h2>دانش‌آموزان</h2><div class='wrap'><table>"
                "<tr><th>نام</th><th>گروه</th><th>به‌موقع</th><th>با تاخیر</th><th>ارسال‌نشده</th><th>درصد</th><th>امتیاز</th><th>نشان‌ها</th></tr>"
                + "".join(rows) + "</table></div><h2>تکالیف</h2><div class='wrap'><table>"
                "<tr><th>تکلیف</th><th>نوع</th><th>مهلت</th><th>ارسال‌شده</th></tr>" + "".join(arows) + "</table></div>")
        return page("خانه", body, cfg.org_name)

    def file_html(sub_id) -> str:
        out = []
        for f in repo.sub_files(sub_id):
            if not f["local_path"]:
                out.append("<small class='muted'>(فایل روی سرور نیست)</small>")
            elif f["tg_kind"] == "photo" or (f["name"] or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                out.append(f"<a href='/file/{f['id']}' target='_blank'><img class='th' src='/file/{f['id']}'></a>")
            else:
                out.append(f"<a href='/file/{f['id']}' target='_blank'>📄 {esc(f['name'] or 'فایل')}</a>")
        return " ".join(out)

    async def assignment(request):
        a = repo.assignment(int(request.match_info["id"]))
        if not a:
            raise web.HTTPNotFound()
        now = utcnow()
        rows = []
        for s in repo.students_for(a["id"]):
            if not repo.counts_for(a, s):
                continue
            o, sub_at, _ = repo.evaluate(a, s["id"], now)
            finals = repo.final_attempts(a["id"], s["id"])
            last = finals[-1] if finals else None
            fb = next((f["feedback"] for f in reversed(finals) if f["feedback"]), "")
            rows.append(f"<tr><td class='l'><a href='/student/{s['id']}'>{esc(s['full_name'])}</a></td>{status_cell(o)}"
                        f"<td>{fmt_dt(sub_at, cfg.tz) if sub_at else '—'}</td><td>{fa(len(finals))}</td>"
                        f"<td class='l'>{file_html(last['id']) if last else ''}</td><td class='l'>{esc(fb)}</td></tr>")
        body = (f"<div class='box'><b>{esc(a['title'])}</b> ({KIND_FA[a['kind']]})<br>مهلت: {fmt_dt(from_db(a['deadline']), cfg.tz)}"
                f"<br>{esc(a['description'] or '')}</div><div class='wrap'><table><tr><th>دانش‌آموز</th><th>وضعیت</th>"
                "<th>زمان آخرین ارسال</th><th>تعداد ارسال</th><th>پاسخ</th><th>بازخورد</th></tr>" + "".join(rows) + "</table></div>")
        return page(a["title"], body, cfg.org_name)

    async def student(request):
        sid = int(request.match_info["id"])
        s = next((x for x in repo.students() if x["id"] == sid), None)   # شامل ستون groups
        if not s:
            raise web.HTTPNotFound()
        now = utcnow()
        sm = repo.summary(s["id"], now)
        rows = []
        for a in repo.assignments_for(s["id"], include_archived=True):
            if not repo.counts_for(a, s):
                continue
            o, sub_at, dl = repo.evaluate(a, s["id"], now)
            rows.append(f"<tr><td class='l'><a href='/assignment/{a['id']}'>{esc(a['title'])}</a></td><td>{KIND_FA[a['kind']]}</td>"
                        f"<td>{fmt_dt(dl, cfg.tz)}</td>{status_cell(o)}<td>{fmt_dt(sub_at, cfg.tz) if sub_at else '—'}</td></tr>")
        badges = "، ".join(b["title"] for b in repo.student_badges(s["id"])) or "هنوز نشانی نگرفته"
        body = (f"<div class='box'><b>{esc(s['full_name'])}</b> — {esc(s['groups'] or '')}<br>نشان‌ها: {esc(badges)}<br>"
                f"امتیاز: {fa(sm.points)} — مسیر نشان بعدی: {fa(sm.run)} از {fa(STREAK_LEN)}</div>"
                "<div class='wrap'><table><tr><th>تکلیف</th><th>نوع</th><th>مهلت</th><th>وضعیت</th><th>زمان ارسال</th></tr>"
                + "".join(rows) + "</table></div>")
        return page(s["full_name"], body, cfg.org_name)

    async def file(request):
        r = repo.one("SELECT * FROM submission_files WHERE id=?", (int(request.match_info["id"]),))
        if not r or not r["local_path"]:
            raise web.HTTPNotFound()
        path = Path(r["local_path"]).resolve()
        if cfg.files_dir.resolve() not in path.parents or not path.exists():   # جلوگیری از خروج از پوشهٔ فایل‌ها
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def export(request):
        with tempfile.TemporaryDirectory() as d:
            p = build_excel(repo, cfg, Path(d) / "assignment_stats.xlsx", utcnow())
            data = p.read_bytes()
        return web.Response(body=data, headers={
            "Content-Disposition": "attachment; filename=assignment_stats.xlsx",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/login", login_get), web.post("/login", login_post), web.get("/logout", logout),
        web.get("/logo.png", logo), web.get("/", dashboard), web.get("/assignment/{id:\\d+}", assignment),
        web.get("/student/{id:\\d+}", student), web.get("/file/{id:\\d+}", file), web.get("/export.xlsx", export),
    ])
    return app


async def start_panel(cfg, repo):
    if len(cfg.panel_password) < 6:
        log.warning("PANEL_PASSWORD کوتاه است؛ حداقل ۸ نویسه انتخاب کن.")
    if cfg.panel_host != "127.0.0.1":
        log.warning("پنل روی %s باز است؛ فقط در شبکهٔ مطمئن یا پشت HTTPS استفاده کن.", cfg.panel_host)
    runner = web.AppRunner(make_app(cfg, repo))
    await runner.setup()
    await web.TCPSite(runner, cfg.panel_host, cfg.panel_port).start()
    log.info("پنل: http://%s:%s", cfg.panel_host, cfg.panel_port)
    return runner
