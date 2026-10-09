"""لایهٔ داده: SQLite (برای ۲۰ تا ۴۰ دانش‌آموز کاملاً کافی و بدون نیاز به سرور جدا)."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta

from . import rules
from .timeutil import from_db, to_db, utcnow

HARD_CLOSE_HOURS = 24  # بعد از این مدت از مهلت، ارسال بسته می‌شود

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, code TEXT NOT NULL,
  chat_id INTEGER UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS student_groups(
  student_id INTEGER NOT NULL, group_id INTEGER NOT NULL, PRIMARY KEY(student_id, group_id));
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, kind TEXT NOT NULL DEFAULT 'regular',
  deadline TEXT NOT NULL, grace_min INTEGER NOT NULL DEFAULT 20, bonus_points INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment_targets(
  assignment_id INTEGER NOT NULL, kind TEXT NOT NULL, ref_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS assignment_files(
  id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, tg_kind TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT);
CREATE TABLE IF NOT EXISTS deadline_overrides(
  assignment_id INTEGER NOT NULL, student_id INTEGER NOT NULL, deadline TEXT NOT NULL,
  PRIMARY KEY(assignment_id, student_id));
CREATE TABLE IF NOT EXISTS excuses(
  assignment_id INTEGER NOT NULL, student_id INTEGER NOT NULL, note TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(assignment_id, student_id));
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
  attempt_no INTEGER NOT NULL, is_draft INTEGER NOT NULL DEFAULT 1, submitted_at TEXT,
  review TEXT NOT NULL DEFAULT 'pending', via_resubmit INTEGER NOT NULL DEFAULT 0, feedback TEXT, feedback_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS submission_files(
  id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL, tg_kind TEXT NOT NULL, file_id TEXT NOT NULL,
  local_path TEXT, name TEXT, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS badges(
  student_id INTEGER NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL, awarded_at TEXT NOT NULL,
  PRIMARY KEY(student_id, key));
CREATE TABLE IF NOT EXISTS resubmit_windows(
  assignment_id INTEGER NOT NULL, student_id INTEGER NOT NULL, until TEXT NOT NULL, note TEXT,
  PRIMARY KEY(assignment_id, student_id));
CREATE TABLE IF NOT EXISTS sessions(chat_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_sub ON submissions(assignment_id, student_id);
"""


class Repo:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        try:  # مهاجرت برای دیتابیس‌های قدیمی
            self.c.execute("ALTER TABLE submissions ADD COLUMN via_resubmit INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

    # ---- helpers
    def q(self, sql, a=()):
        return self.c.execute(sql, a).fetchall()

    def one(self, sql, a=()):
        return self.c.execute(sql, a).fetchone()

    def x(self, sql, a=()):
        cur = self.c.execute(sql, a)
        self.c.commit()
        return cur.lastrowid

    # ---- meta / sessions
    def meta_get(self, key):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def meta_set(self, key, value):
        self.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def get_session(self, chat):
        r = self.one("SELECT state,data FROM sessions WHERE chat_id=?", (chat,))
        return (r["state"], json.loads(r["data"])) if r else ("", {})

    def set_session(self, chat, state, data=None):
        self.x("INSERT OR REPLACE INTO sessions VALUES(?,?,?)", (chat, state, json.dumps(data or {}, ensure_ascii=False)))

    def clear_session(self, chat):
        self.x("DELETE FROM sessions WHERE chat_id=?", (chat,))

    # ---- groups
    def add_group(self, name):
        r = self.one("SELECT id FROM groups WHERE name=?", (name,))
        return r["id"] if r else self.x("INSERT INTO groups(name) VALUES(?)", (name,))

    def groups(self):
        return self.q("SELECT g.*, (SELECT COUNT(*) FROM student_groups sg JOIN students s ON s.id=sg.student_id "
                      "WHERE sg.group_id=g.id AND s.active=1) AS n FROM groups g ORDER BY g.name")

    def groups_with_open_slots(self):
        return self.q("SELECT g.* FROM groups g WHERE EXISTS (SELECT 1 FROM student_groups sg JOIN students s "
                      "ON s.id=sg.student_id WHERE sg.group_id=g.id AND s.chat_id IS NULL AND s.active=1) ORDER BY g.name")

    def unlinked_members(self, gid):
        return self.q("SELECT s.* FROM students s JOIN student_groups sg ON sg.student_id=s.id "
                      "WHERE sg.group_id=? AND s.chat_id IS NULL AND s.active=1 ORDER BY s.full_name", (gid,))

    # ---- students
    def _new_code(self):
        while True:
            code = str(secrets.randbelow(900000) + 100000)
            if not self.one("SELECT 1 FROM students WHERE code=?", (code,)):
                return code

    def add_student(self, name, group_ids):
        code = self._new_code()
        sid = self.x("INSERT INTO students(full_name,code,created_at) VALUES(?,?,?)", (name, code, to_db(utcnow())))
        for g in group_ids:
            self.c.execute("INSERT OR IGNORE INTO student_groups VALUES(?,?)", (sid, g))
        self.c.commit()
        return sid, code

    def student(self, sid):
        return self.one("SELECT * FROM students WHERE id=?", (sid,))

    def student_by_chat(self, chat):
        return self.one("SELECT * FROM students WHERE chat_id=? AND active=1", (chat,))

    def link_student(self, sid, chat):
        self.x("UPDATE students SET chat_id=? WHERE id=?", (chat, sid))

    def students(self):
        return self.q("SELECT s.*, (SELECT GROUP_CONCAT(g.name, '، ') FROM student_groups sg JOIN groups g "
                      "ON g.id=sg.group_id WHERE sg.student_id=s.id) AS groups FROM students s "
                      "WHERE s.active=1 ORDER BY s.full_name")

    # ---- assignments
    def create_assignment(self, title, desc, kind, deadline_db, grace, points, targets, files):
        aid = self.c.execute(
            "INSERT INTO assignments(title,description,kind,deadline,grace_min,bonus_points,created_at) VALUES(?,?,?,?,?,?,?)",
            (title, desc, kind, deadline_db, grace, points, to_db(utcnow()))).lastrowid
        for k, ref in targets:
            self.c.execute("INSERT INTO assignment_targets VALUES(?,?,?)", (aid, k, ref))
        for f in files:
            self.c.execute("INSERT INTO assignment_files(assignment_id,tg_kind,file_id,name) VALUES(?,?,?,?)",
                           (aid, f["kind"], f["file_id"], f.get("name")))
        self.c.commit()
        return aid

    def assignment(self, aid):
        return self.one("SELECT * FROM assignments WHERE id=?", (aid,))

    def assignment_files(self, aid):
        return self.q("SELECT * FROM assignment_files WHERE assignment_id=? ORDER BY id", (aid,))

    def assignments_for(self, sid, include_archived=False):
        st = "" if include_archived else "a.status='active' AND "
        return self.q(
            f"SELECT a.* FROM assignments a WHERE {st}("
            "EXISTS(SELECT 1 FROM assignment_targets t WHERE t.assignment_id=a.id AND t.kind='all') OR "
            "EXISTS(SELECT 1 FROM assignment_targets t JOIN student_groups sg ON sg.group_id=t.ref_id "
            "WHERE t.assignment_id=a.id AND t.kind='group' AND sg.student_id=?)) ORDER BY a.deadline DESC", (sid,))

    def active_assignments(self):
        return self.q("SELECT * FROM assignments WHERE status='active' ORDER BY deadline DESC")

    def students_for(self, aid):
        if self.one("SELECT 1 FROM assignment_targets WHERE assignment_id=? AND kind='all'", (aid,)):
            return self.q("SELECT * FROM students WHERE active=1 ORDER BY full_name")
        return self.q("SELECT DISTINCT s.* FROM students s JOIN student_groups sg ON sg.student_id=s.id "
                      "JOIN assignment_targets t ON t.ref_id=sg.group_id AND t.kind='group' "
                      "WHERE t.assignment_id=? AND s.active=1 ORDER BY s.full_name", (aid,))

    def effective_deadline(self, a, sid):
        r = self.one("SELECT deadline FROM deadline_overrides WHERE assignment_id=? AND student_id=?", (a["id"], sid))
        return from_db(r["deadline"] if r else a["deadline"])

    # ---- submissions
    def final_attempts(self, aid, sid):
        return self.q("SELECT * FROM submissions WHERE assignment_id=? AND student_id=? AND is_draft=0 "
                      "ORDER BY attempt_no", (aid, sid))

    def draft(self, aid, sid):
        return self.one("SELECT * FROM submissions WHERE assignment_id=? AND student_id=? AND is_draft=1", (aid, sid))

    def get_or_create_draft(self, aid, sid):
        d = self.draft(aid, sid)
        if d:
            return d["id"]
        n = len(self.final_attempts(aid, sid)) + 1
        return self.x("INSERT INTO submissions(assignment_id,student_id,attempt_no,is_draft,created_at) VALUES(?,?,?,1,?)",
                      (aid, sid, n, to_db(utcnow())))

    def add_sub_file(self, sub_id, tg_kind, file_id, local_path, name, received_at):
        self.x("INSERT INTO submission_files(submission_id,tg_kind,file_id,local_path,name,received_at) VALUES(?,?,?,?,?,?)",
               (sub_id, tg_kind, file_id, local_path, name, to_db(received_at)))

    def sub_files(self, sub_id):
        return self.q("SELECT * FROM submission_files WHERE submission_id=? ORDER BY id", (sub_id,))

    def finalize(self, sub_id):
        r = self.one("SELECT MAX(received_at) AS t FROM submission_files WHERE submission_id=?", (sub_id,))
        if not r or not r["t"]:
            return None
        sub = self.one("SELECT * FROM submissions WHERE id=?", (sub_id,))
        a = self.assignment(sub["assignment_id"])
        at = from_db(r["t"])
        # ارسالی که بعد از مهلت و داخل «پنجرهٔ ارسال مجدد» مدیر انجام شود، تاخیر محسوب نمی‌شود
        w = self.resubmit_window(a["id"], sub["student_id"])
        late = at > self.effective_deadline(a, sub["student_id"]) + timedelta(minutes=a["grace_min"])
        via = int(bool(w and late and at <= from_db(w["until"]) and sub["attempt_no"] > 1))
        self.x("UPDATE submissions SET is_draft=0, submitted_at=?, via_resubmit=? WHERE id=?", (r["t"], via, sub_id))
        return at

    def discard_draft(self, sub_id):
        self.c.execute("DELETE FROM submission_files WHERE submission_id=?", (sub_id,))
        self.c.execute("DELETE FROM submissions WHERE id=? AND is_draft=1", (sub_id,))
        self.c.commit()

    def resubmit_window(self, aid, sid):
        r = self.one("SELECT * FROM resubmit_windows WHERE assignment_id=? AND student_id=?", (aid, sid))
        return r

    def can_submit(self, a, sid, now):
        """
        - بعد از «مهلت + ۲۴ ساعت» هیچ ارسالی پذیرفته نمی‌شود (مگر مدیر مهلت را تمدید کند).
        - اگر قبلاً ارسال کرده: تا پایان مهلت ارفاقی می‌تواند جایگزین کند؛ بعدش فقط با «درخواست ارسال مجدد» مدیر
          که یک پنجرهٔ ۲۴ ساعته از لحظهٔ درخواست باز می‌کند.
        """
        dl = self.effective_deadline(a, sid)
        w = self.resubmit_window(a["id"], sid)
        if w and now <= from_db(w["until"]):
            return True
        if now > dl + timedelta(hours=HARD_CLOSE_HOURS):
            return False
        if not self.final_attempts(a["id"], sid):
            return True
        return now <= dl + timedelta(minutes=a["grace_min"])

    # ---- admin actions
    def latest_final(self, aid, sid):
        f = self.final_attempts(aid, sid)
        return f[-1] if f else None

    def set_feedback(self, sub_id, text):
        self.x("UPDATE submissions SET feedback=?, feedback_at=? WHERE id=?", (text, to_db(utcnow()), sub_id))

    def request_resubmit(self, aid, sid, note, hours=HARD_CLOSE_HOURS):
        until = to_db(utcnow() + timedelta(hours=hours))
        self.x("INSERT OR REPLACE INTO resubmit_windows VALUES(?,?,?,?)", (aid, sid, until, note))
        last = self.latest_final(aid, sid)
        if last:
            self.x("UPDATE submissions SET review='resubmit' WHERE id=?", (last["id"],))
        return from_db(until)

    def set_override(self, aid, sid, deadline_db):
        self.x("INSERT OR REPLACE INTO deadline_overrides VALUES(?,?,?)", (aid, sid, deadline_db))

    def is_excused(self, aid, sid):
        return bool(self.one("SELECT 1 FROM excuses WHERE assignment_id=? AND student_id=?", (aid, sid)))

    def toggle_excuse(self, aid, sid):
        if self.is_excused(aid, sid):
            self.x("DELETE FROM excuses WHERE assignment_id=? AND student_id=?", (aid, sid))
            return False
        self.x("INSERT INTO excuses VALUES(?,?,?,?)", (aid, sid, None, to_db(utcnow())))
        return True

    def update_assignment(self, aid, **fields):
        for k in fields:
            assert k in ("title", "description", "deadline", "status")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE assignments SET {sets} WHERE id=?", (*fields.values(), aid))

    def registered_students(self, group_ids=None):
        if not group_ids:
            return self.q("SELECT * FROM students WHERE active=1 AND chat_id IS NOT NULL")
        qm = ",".join("?" * len(group_ids))
        return self.q(f"SELECT DISTINCT s.* FROM students s JOIN student_groups sg ON sg.student_id=s.id "
                      f"WHERE s.active=1 AND s.chat_id IS NOT NULL AND sg.group_id IN ({qm})", tuple(group_ids))

    # ---- evaluation
    def evaluate(self, a, sid, now):
        """زمان ارسال = آخرین ارسال نهایی دانش‌آموز؛ به‌جز ارسال‌هایی که با درخواست مدیر (ارسال مجدد) بعد از مهلت بوده‌اند."""
        finals = self.final_attempts(a["id"], sid)
        counted = [f for f in finals if not f["via_resubmit"]] or finals
        sub_at = from_db(counted[-1]["submitted_at"]) if counted else None
        dl = self.effective_deadline(a, sid)
        ex = self.is_excused(a["id"], sid)
        return rules.classify(sub_at, dl, a["grace_min"], now, ex), sub_at, dl

    def counts_for(self, a, s):
        """تکلیف‌هایی که قبل از عضویت دانش‌آموز ساخته شده‌اند در آمار او حساب نمی‌شوند."""
        return a["created_at"] >= s["created_at"]

    def records(self, sid, now):
        s = self.student(sid)
        out = []
        for a in self.assignments_for(sid, include_archived=True):
            if not self.counts_for(a, s):
                continue
            o, sub_at, dl = self.evaluate(a, sid, now)
            out.append(rules.Record(a["kind"], dl, o.status, sub_at, a["bonus_points"]))
        return out

    def summary(self, sid, now):
        return rules.summarize(self.records(sid, now))

    # ---- badges
    def award(self, sid, badges):
        new = []
        for key, title in badges.items():
            cur = self.c.execute("INSERT OR IGNORE INTO badges VALUES(?,?,?,?)", (sid, key, title, to_db(utcnow())))
            if cur.rowcount:
                new.append(title)
        self.c.commit()
        return new

    def student_badges(self, sid):
        return self.q("SELECT * FROM badges WHERE student_id=? ORDER BY awarded_at", (sid,))
