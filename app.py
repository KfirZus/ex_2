"""Smart Classroom - Flask web application (Hebrew / RTL).
Run:  python app.py   ->  http://<your-ip>:5000
"""
import csv
import io
import ipaddress
import json
import os
import random
import socket
import sqlite3
import string
import time
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import env_loader  # noqa: F401  (loads .env into os.environ; must come before ai)
import ai

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SC_DATA", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
DB_PATH = os.path.join(DATA, "classroom.db")
os.makedirs(UPLOADS, exist_ok=True)

ATTENDANCE_SECONDS = 10 * 60
Q_ANSWER, Q_REVEAL = 10, 5  # poll timings (seconds)
TEACHER_CODE = os.environ.get("TEACHER_CODE", "teacher123")
ALLOWED_NETS = [ipaddress.ip_network(n.strip(), strict=False) for n in os.environ.get(
    "ALLOWED_NETWORKS",
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16").split(",")]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw TEXT, role TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS courses(id INTEGER PRIMARY KEY, name TEXT, day TEXT, time TEXT, code TEXT UNIQUE, teacher_id INT, end_time TEXT);
CREATE TABLE IF NOT EXISTS enroll(course_id INT, user_id INT, PRIMARY KEY(course_id,user_id));
CREATE TABLE IF NOT EXISTS att_sessions(id INTEGER PRIMARY KEY, course_id INT, started REAL, ends REAL);
CREATE TABLE IF NOT EXISTS att_records(session_id INT, user_id INT, ts REAL, PRIMARY KEY(session_id,user_id));
CREATE TABLE IF NOT EXISTS att_photos(id INTEGER PRIMARY KEY, course_id INT, session_id INT, filename TEXT, taken REAL, ai_count INT, registered INT);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, course_id INT, topic TEXT, date TEXT, start TEXT);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, lesson_id INT, user_id INT, label TEXT, body TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY, course_id INT, title TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, poll_id INT, started REAL);
CREATE TABLE IF NOT EXISTS answers(run_id INT, qidx INT, user_id INT, opt INT, PRIMARY KEY(run_id,qidx,user_id));
CREATE TABLE IF NOT EXISTS recordings(id INTEGER PRIMARY KEY, course_id INT, filename TEXT, transcript TEXT, summary TEXT, uploaded REAL);
CREATE TABLE IF NOT EXISTS boxes(id INTEGER PRIMARY KEY, course_id INT, name TEXT, due REAL);
CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, box_id INT, user_id INT, filename TEXT, orig TEXT, ts REAL, text TEXT);
CREATE TABLE IF NOT EXISTS announcements(id INTEGER PRIMARY KEY, course_id INT, body TEXT, ts REAL);
"""


# ------------------------------------------------------------------ helpers
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def q(sql, *args, one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def run(sql, *args):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


DAYS = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
DEFAULT_COURSE_MINUTES = 120  # end time given to courses created before end_time existed


def to_minutes(hhmm):
    """'HH:MM' -> minutes since midnight, or None if invalid."""
    try:
        h, m = (int(x) for x in (hhmm or "").split(":"))
    except ValueError:
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def hhmm(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def default_end(start):
    s = to_minutes(start)
    return hhmm(min(s + DEFAULT_COURSE_MINUTES, 23 * 60 + 59)) if s is not None else None


def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    if "end_time" not in [r[1] for r in c.execute("PRAGMA table_info(courses)")]:
        c.execute("ALTER TABLE courses ADD COLUMN end_time TEXT")  # databases from before end_time
    for cid, start in c.execute("SELECT id, time FROM courses WHERE end_time IS NULL").fetchall():
        c.execute("UPDATE courses SET end_time=? WHERE id=?", (default_end(start), cid))
    c.commit()
    c.close()


def fmt(ts, f="%d/%m/%Y %H:%M"):
    return datetime.fromtimestamp(ts).strftime(f) if ts else ""


app.jinja_env.filters["dt"] = fmt


def parse_dt(s, f):
    """strptime that returns None instead of raising on bad / missing input."""
    try:
        return datetime.strptime(s, f)
    except (TypeError, ValueError):
        return None


def is_past(when):
    """True if `when` is earlier than the current minute."""
    return when < datetime.now().replace(second=0, microsecond=0)


def current_user():
    uid = session.get("uid")
    return q("SELECT * FROM users WHERE id=?", uid, one=True) if uid else None


@app.before_request
def load_user():
    g.user = current_user()


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            # remember where the user was going (e.g. a scanned /join?code=... link)
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return f(*a, **k)
    return w


def safe_next(default="courses"):
    """Return the ?next= target only if it is a local path (no open redirect)."""
    n = request.values.get("next", "")
    if n.startswith("/") and not n.startswith("//") and "\\" not in n:
        return n
    return url_for(default)


def lan_ip():
    """The machine's LAN address, so a phone can reach the server (localhost would not work)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def public_join_url(code):
    """Join link for the QR. If the teacher opened the site via localhost, a student's phone
    could not open it, so swap in the LAN IP. Set PUBLIC_URL to override (e.g. an https tunnel)."""
    path = url_for("join", code=code)
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if base:
        return base + path
    host = request.host.split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        ip = lan_ip()
        if ip:
            port = request.host.partition(":")[2]
            return "%s://%s%s%s" % (request.scheme, ip, ":" + port if port else "", path)
    return request.host_url.rstrip("/") + path


def course_access(cid, teacher_only=False):
    """Returns course row if the current user may access it."""
    c = q("SELECT * FROM courses WHERE id=?", cid, one=True)
    if not c:
        abort(404)
    if g.user["role"] == "teacher":
        if c["teacher_id"] != g.user["id"]:
            abort(403)
    else:
        if teacher_only or not q("SELECT 1 FROM enroll WHERE course_id=? AND user_id=?",
                                 cid, g.user["id"], one=True):
            abort(403)
    return c


def is_teacher():
    return g.user["role"] == "teacher"


def on_school_network():
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    ip = fwd or request.remote_addr
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in ALLOWED_NETS)


def save_upload(file):
    name = "%s_%s" % (uuid.uuid4().hex[:10], secure_filename(file.filename) or "file")
    file.save(os.path.join(UPLOADS, name))
    return name


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read(200000).decode("utf-8", errors="ignore")
    except OSError:
        return ""


# --------------------------------------------------------------------- auth
@app.route("/")
def index():
    return redirect(url_for("courses") if g.user else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username=?", request.form["username"].strip(), one=True)
        if u and check_password_hash(u["pw"], request.form["password"]):
            session["uid"] = u["id"]
            return redirect(safe_next())
        flash("שם משתמש או סיסמה שגויים", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        username, name = f["username"].strip(), f["name"].strip()
        role = "teacher" if f.get("role") == "teacher" else "student"
        if not username or not f["password"] or not name:
            flash("יש למלא את כל השדות", "error")
        elif role == "teacher" and f.get("teacher_code") != TEACHER_CODE:
            flash("קוד מורה שגוי", "error")
        elif q("SELECT 1 FROM users WHERE username=?", username, one=True):
            flash("שם המשתמש תפוס", "error")
        else:
            uid = run("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)",
                      username, generate_password_hash(f["password"]), role, name)
            session["uid"] = uid
            return redirect(safe_next())
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------- courses
@app.route("/courses")
@login_required
def courses():
    if is_teacher():
        rows = q("SELECT * FROM courses WHERE teacher_id=?", g.user["id"])
    else:
        rows = q("SELECT c.* FROM courses c JOIN enroll e ON e.course_id=c.id WHERE e.user_id=?",
                 g.user["id"])
    return render_template("courses.html", courses=rows)


@app.route("/course/new", methods=["GET", "POST"])
@login_required
def course_new():
    if not is_teacher():
        abort(403)
    form = dict(name="", day=DAYS[0], time="10:00", end_time="12:00")
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        form["end_time"] = form["end_time"] or default_end(form["time"]) or ""
        error = course_form_error(form)
        if not error:
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            cid = run("INSERT INTO courses(name,day,time,end_time,code,teacher_id) VALUES(?,?,?,?,?,?)",
                      form["name"], form["day"], form["time"], form["end_time"], code, g.user["id"])
            return redirect(url_for("course_home", cid=cid))
        flash(error, "error")
    return render_template("course_new.html", form=form, days=DAYS)


def course_form_error(f):
    """Validates a new course. The same teacher may not have two courses whose times overlap on
    the same day; back-to-back courses (one ends 14:00, the next starts 14:00) are allowed."""
    start, end = to_minutes(f["time"]), to_minutes(f["end_time"])
    if not f["name"]:
        return "יש להזין שם לקורס"
    if f["day"] not in DAYS:
        return "יום לא תקין"
    if start is None or end is None:
        return "שעה לא תקינה"
    if end <= start:
        return "שעת הסיום חייבת להיות אחרי שעת ההתחלה"
    mine = q("SELECT * FROM courses WHERE teacher_id=? AND day=?", g.user["id"], f["day"])
    for c in overlapping(mine, f["day"], start, end):
        s2, e2 = course_span(c)
        # ⁦...⁩ keeps "10:00–12:00" left-to-right inside the Hebrew sentence
        return "הקורס \"%s\" כבר קבוע ביום %s בשעות ⁦%s–%s⁩. בחר/י יום או שעה אחרים." % (
            c["name"], f["day"], hhmm(s2), hhmm(e2))
    return None


def course_span(c):
    """(start, end) of a course in minutes since midnight; (None, None) if its time is invalid."""
    s = to_minutes(c["time"])
    e = to_minutes(c["end_time"] or default_end(c["time"]))
    return (s, e) if s is not None and e is not None else (None, None)


def overlapping(courses, day, start, end):
    """Courses from `courses` held on `day` whose hours overlap [start, end).
    Back-to-back is not an overlap: 12:00-14:00 and 14:00-16:00 are fine."""
    out = []
    for c in courses:
        s2, e2 = course_span(c)
        if c["day"] == day and s2 is not None and start < e2 and s2 < end:
            out.append(c)
    return out


def enrolled_clashes(course):
    """The current student's courses that meet on the same day at overlapping hours."""
    start, end = course_span(course)
    if start is None:
        return []
    mine = q("SELECT c.* FROM courses c JOIN enroll e ON e.course_id=c.id "
             "WHERE e.user_id=? AND c.id!=?", g.user["id"], course["id"])
    return overlapping(mine, course["day"], start, end)


@app.route("/join", methods=["GET", "POST"])
@login_required
def join():
    if is_teacher():
        abort(403)
    code = (request.values.get("code") or "").strip().upper()
    if code:
        c = q("SELECT * FROM courses WHERE code=?", code, one=True)
        if c:
            enrolled = q("SELECT 1 FROM enroll WHERE course_id=? AND user_id=?",
                         c["id"], g.user["id"], one=True)
            if not enrolled and request.values.get("confirm") != "1":
                clashes = enrolled_clashes(c)
                if clashes:  # warn and ask before enrolling in a course at the same hours
                    return render_template("join_confirm.html", c=c, clashes=clashes,
                                           span=course_span(c), hhmm=hhmm)
            db().execute("INSERT OR IGNORE INTO enroll VALUES(?,?)", (c["id"], g.user["id"]))
            db().commit()
            return redirect(url_for("course_home", cid=c["id"]))
        flash("קוד קורס לא נמצא", "error")
    return render_template("join.html")


@app.route("/course/<int:cid>")
@login_required
def course_home(cid):
    c = course_access(cid)
    return render_template("course_home.html", c=c)


@app.route("/course/<int:cid>/add-people")
@login_required
def add_people(cid):
    c = course_access(cid, teacher_only=True)
    join_url = public_join_url(c["code"])
    return render_template("add_people.html", c=c, join_url=join_url)


# -------------------------------------------------------------- attendance
def active_session(cid):
    return q("SELECT * FROM att_sessions WHERE course_id=? AND ends>? ORDER BY id DESC",
             cid, time.time(), one=True)


@app.route("/course/<int:cid>/attendance")
@login_required
def attendance(cid):
    c = course_access(cid)
    sess = active_session(cid)
    ctx = dict(c=c, active=sess, wifi_ok=on_school_network())
    if is_teacher():
        rows = q("""SELECT s.*, (SELECT COUNT(*) FROM att_records r WHERE r.session_id=s.id) cnt
                    FROM att_sessions s WHERE course_id=? ORDER BY id DESC""", cid)
        ctx["sessions"] = rows
        ctx["photos"] = q("SELECT p.id,p.course_id,p.session_id,p.filename,p.taken,p.ai_count,(SELECT COUNT(*) FROM att_records r WHERE r.session_id=p.session_id) registered FROM att_photos p WHERE p.course_id=? ORDER BY p.id DESC", cid)
        return render_template("attendance_teacher.html", **ctx)
    sessions = q("SELECT * FROM att_sessions WHERE course_id=? ORDER BY id DESC", cid)
    present = {r["session_id"] for r in q(
        "SELECT session_id FROM att_records WHERE user_id=?", g.user["id"])}
    ctx["history"] = [(s, s["id"] in present) for s in sessions if s["ends"] < time.time()
                      or s["id"] in present]
    ctx["photos"] = q("SELECT p.id,p.course_id,p.session_id,p.filename,p.taken,p.ai_count,(SELECT COUNT(*) FROM att_records r WHERE r.session_id=p.session_id) registered FROM att_photos p WHERE p.course_id=? ORDER BY p.id DESC", cid)
    ctx["checked"] = sess and sess["id"] in present
    return render_template("attendance_student.html", **ctx)


@app.route("/course/<int:cid>/attendance/start", methods=["POST"])
@login_required
def attendance_start(cid):
    course_access(cid, teacher_only=True)
    if not active_session(cid):
        now = time.time()
        run("INSERT INTO att_sessions(course_id,started,ends) VALUES(?,?,?)",
            cid, now, now + ATTENDANCE_SECONDS)
    return redirect(url_for("attendance", cid=cid))


@app.route("/course/<int:cid>/attendance/checkin", methods=["POST"])
@login_required
def attendance_checkin(cid):
    course_access(cid)
    sess = active_session(cid)
    # Per spec: outside the Wi-Fi or the timer window nothing happens.
    if is_teacher() or not sess or not on_school_network():
        return jsonify(ok=False)
    db().execute("INSERT OR IGNORE INTO att_records VALUES(?,?,?)",
                 (sess["id"], g.user["id"], time.time()))
    db().commit()
    return jsonify(ok=True)


@app.route("/course/<int:cid>/attendance/status")
@login_required
def attendance_status(cid):
    course_access(cid)
    s = active_session(cid)
    return jsonify(active=bool(s), remaining=int(s["ends"] - time.time()) if s else 0,
                   count=q("SELECT COUNT(*) n FROM att_records WHERE session_id=?",
                           s["id"], one=True)["n"] if s else 0)


@app.route("/course/<int:cid>/attendance/photo", methods=["POST"])
@login_required
def attendance_photo(cid):
    course_access(cid, teacher_only=True)
    f = request.files.get("photo")
    if not f or not f.filename:
        flash("לא נבחרה תמונה", "error")
        return redirect(url_for("attendance", cid=cid))
    name = save_upload(f)
    sess = q("SELECT * FROM att_sessions WHERE course_id=? ORDER BY id DESC", cid, one=True)
    count = ai.count_people(os.path.join(UPLOADS, name))
    registered = q("SELECT COUNT(*) n FROM att_records WHERE session_id=?", sess["id"],
                   one=True)["n"] if sess else 0
    run("INSERT INTO att_photos(course_id,session_id,filename,taken,ai_count,registered) "
        "VALUES(?,?,?,?,?,?)", cid, sess["id"] if sess else None, name, time.time(), count,
        registered)
    return redirect(url_for("attendance", cid=cid))


@app.route("/course/<int:cid>/attendance/photo/<int:pid>/delete", methods=["POST"])
@login_required
def photo_delete(cid, pid):
    course_access(cid, teacher_only=True)
    run("DELETE FROM att_photos WHERE id=? AND course_id=?", pid, cid)
    return redirect(url_for("attendance", cid=cid))


@app.route("/course/<int:cid>/attendance/export.csv")
@login_required
def attendance_export(cid):
    """The 'most important feature': spreadsheet with per-student attendance."""
    c = course_access(cid, teacher_only=True)
    sessions = q("SELECT * FROM att_sessions WHERE course_id=? ORDER BY id", cid)
    students = q("SELECT u.* FROM users u JOIN enroll e ON e.user_id=u.id WHERE e.course_id=? "
                 "ORDER BY u.name", cid)
    present = {(r["session_id"], r["user_id"]) for r in q(
        "SELECT * FROM att_records WHERE session_id IN (SELECT id FROM att_sessions "
        "WHERE course_id=?)", cid)}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["שם", "שם משתמש"] + [fmt(s["started"]) for s in sessions] + ["סה\"כ נוכחות", "אחוז"])
    for st in students:
        marks = [(s["id"], st["id"]) in present for s in sessions]
        n = sum(marks)
        w.writerow([st["name"], st["username"]] + ["V" if m else "X" for m in marks] +
                   [n, "%d%%" % (100 * n / len(sessions)) if sessions else "0%"])
    data = "﻿" + out.getvalue()  # BOM so Excel opens Hebrew correctly
    return Response(data, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=attendance_course_%d.csv" % cid})


# ------------------------------------------------------------------- notes
@app.route("/course/<int:cid>/notes", methods=["GET", "POST"])
@login_required
def notes(cid):
    c = course_access(cid)
    if request.method == "POST":
        if not is_teacher():
            abort(403)
        f = request.form
        when = parse_dt("%s %s" % (f.get("date"), f.get("start")), "%Y-%m-%d %H:%M")
        if not when:
            flash("תאריך או שעה לא תקינים", "error")
        elif is_past(when):
            flash("לא ניתן לקבוע שיעור לתאריך או לשעה שכבר עברו", "error")
        else:
            run("INSERT INTO lessons(course_id,topic,date,start) VALUES(?,?,?,?)",
                cid, f["topic"], f["date"], f["start"])
            flash("קבעת שיעור לתאריך %s בנושא %s האפשרות להגיב תתחיל בשעה %s" % (
                when.strftime("%d/%m/%Y"), f["topic"], f["start"]), "ok")
        return redirect(url_for("notes", cid=cid))
    lessons = q("SELECT * FROM lessons WHERE course_id=? ORDER BY date DESC, start DESC", cid)
    return render_template("notes.html", c=c, lessons=lessons)


def lesson_open(lesson):
    start = datetime.strptime(lesson["date"] + " " + lesson["start"], "%Y-%m-%d %H:%M")
    return datetime.now() >= start


@app.route("/lesson/<int:lid>")
@login_required
def lesson(lid):
    l = q("SELECT * FROM lessons WHERE id=?", lid, one=True) or abort(404)
    c = course_access(l["course_id"])
    return render_template("lesson.html", c=c, l=l, open=lesson_open(l) or is_teacher())


@app.route("/lesson/<int:lid>/comments", methods=["GET", "POST"])
@login_required
def lesson_comments(lid):
    l = q("SELECT * FROM lessons WHERE id=?", lid, one=True) or abort(404)
    course_access(l["course_id"])
    if request.method == "POST":
        body = (request.json or {}).get("body", "").strip()[:200]
        anon = (request.json or {}).get("anon")
        if not body or not (lesson_open(l) or is_teacher()):
            return jsonify(ok=False), 400
        label = g.user["name"] if (is_teacher() or not anon) else "student"
        run("INSERT INTO comments(lesson_id,user_id,label,body,ts) VALUES(?,?,?,?,?)",
            lid, g.user["id"], label, body, time.time())
        return jsonify(ok=True)
    rows = q("SELECT c.label, c.body, c.ts, u.role FROM comments c JOIN users u ON u.id=c.user_id "
             "WHERE lesson_id=? ORDER BY c.id", lid)
    return jsonify([dict(label=r["label"], body=r["body"], time=fmt(r["ts"], "%d/%m %H:%M"),
                         teacher=r["role"] == "teacher") for r in rows])


# ------------------------------------------------------------------- polls
def poll_run_state(run_row, poll):
    qs = json.loads(poll["data"])
    elapsed = time.time() - run_row["started"]
    step = Q_ANSWER + Q_REVEAL
    idx = int(elapsed // step)
    if idx >= len(qs):
        return dict(finished=True, total=len(qs))
    within = elapsed - idx * step
    phase = "answer" if within < Q_ANSWER else "reveal"
    remaining = int((Q_ANSWER - within) if phase == "answer" else (step - within)) + 1
    return dict(finished=False, idx=idx, phase=phase, remaining=remaining, total=len(qs))


def run_counts(run_id, qidx, n_opts):
    rows = q("SELECT opt, COUNT(*) n FROM answers WHERE run_id=? AND qidx=? GROUP BY opt",
             run_id, qidx)
    counts = [0] * n_opts
    for r in rows:
        if 0 <= r["opt"] < n_opts:
            counts[r["opt"]] = r["n"]
    return counts


@app.route("/course/<int:cid>/polls")
@login_required
def polls(cid):
    c = course_access(cid)
    ps = q("SELECT * FROM polls WHERE course_id=? ORDER BY id DESC", cid)
    live = None
    for r in q("SELECT r.*, p.id pid FROM runs r JOIN polls p ON p.id=r.poll_id "
               "WHERE p.course_id=? ORDER BY r.id DESC LIMIT 1", cid):
        p = q("SELECT * FROM polls WHERE id=?", r["pid"], one=True)
        if not poll_run_state(r, p)["finished"]:
            live = r
    return render_template("polls.html", c=c, polls=ps, live=live,
                           counts={p["id"]: len(json.loads(p["data"])) for p in ps})


@app.route("/course/<int:cid>/polls/new")
@login_required
def poll_new(cid):
    c = course_access(cid, teacher_only=True)
    return render_template("poll_new.html", c=c)


@app.route("/course/<int:cid>/polls/save", methods=["POST"])
@login_required
def poll_save(cid):
    course_access(cid, teacher_only=True)
    d = request.json or {}
    qs = []
    for item in d.get("questions", []):
        opts = [o for o in item.get("options", []) if o.get("text", "").strip()][:4]
        text = item.get("text", "").strip()
        if not text or len(opts) < 2:
            return jsonify(ok=False, error="לכל שאלה נדרשים טקסט ולפחות שתי תשובות"), 400
        correct = next((i for i, o in enumerate(opts) if o.get("correct")), 0)
        qs.append(dict(text=text, options=[o["text"].strip() for o in opts], correct=correct))
    if not qs:
        return jsonify(ok=False, error="אין שאלות"), 400
    run("INSERT INTO polls(course_id,title,data) VALUES(?,?,?)", cid,
        d.get("title") or qs[0]["text"][:40], json.dumps(qs, ensure_ascii=False))
    return jsonify(ok=True, url=url_for("polls", cid=cid))


@app.route("/course/<int:cid>/polls/<int:pid>/delete", methods=["POST"])
@login_required
def poll_delete(cid, pid):
    course_access(cid, teacher_only=True)
    run("DELETE FROM polls WHERE id=? AND course_id=?", pid, cid)
    return redirect(url_for("polls", cid=cid))


@app.route("/course/<int:cid>/polls/<int:pid>/start", methods=["POST"])
@login_required
def poll_start(cid, pid):
    course_access(cid, teacher_only=True)
    q("SELECT 1 FROM polls WHERE id=? AND course_id=?", pid, cid, one=True) or abort(404)
    rid = run("INSERT INTO runs(poll_id,started) VALUES(?,?)", pid, time.time() + 2)
    return redirect(url_for("poll_live", rid=rid))


def get_run(rid):
    r = q("SELECT * FROM runs WHERE id=?", rid, one=True) or abort(404)
    p = q("SELECT * FROM polls WHERE id=?", r["poll_id"], one=True)
    c = course_access(p["course_id"])
    return r, p, c


@app.route("/poll-run/<int:rid>")
@login_required
def poll_live(rid):
    r, p, c = get_run(rid)
    return render_template("poll_live.html", r=r, p=p, c=c)


@app.route("/poll-run/<int:rid>/state")
@login_required
def poll_state(rid):
    r, p, c = get_run(rid)
    qs = json.loads(p["data"])
    if r["started"] > time.time():
        return jsonify(finished=False, waiting=True, total=len(qs))
    st = poll_run_state(r, p)
    if st["finished"]:
        return jsonify(st)
    item = qs[st["idx"]]
    st["question"] = item["text"]
    st["options"] = item["options"]
    mine = q("SELECT opt FROM answers WHERE run_id=? AND qidx=? AND user_id=?",
             rid, st["idx"], g.user["id"], one=True)
    st["mine"] = mine["opt"] if mine else None
    if st["phase"] == "reveal" or is_teacher():
        st["counts"] = run_counts(rid, st["idx"], len(item["options"]))
    if st["phase"] == "reveal":
        st["correct"] = item["correct"]
    return jsonify(st)


@app.route("/poll-run/<int:rid>/answer", methods=["POST"])
@login_required
def poll_answer(rid):
    r, p, c = get_run(rid)
    if is_teacher() or r["started"] > time.time():
        return jsonify(ok=False), 400
    st = poll_run_state(r, p)
    if st["finished"] or st["phase"] != "answer":
        return jsonify(ok=False), 400
    opt = int((request.json or {}).get("opt", -1))
    db().execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?)", (rid, st["idx"], g.user["id"], opt))
    db().commit()
    return jsonify(ok=True)


@app.route("/poll/<int:pid>/review")
@login_required
def poll_review(pid):
    p = q("SELECT * FROM polls WHERE id=?", pid, one=True) or abort(404)
    c = course_access(p["course_id"])
    last = q("SELECT * FROM runs WHERE poll_id=? ORDER BY id DESC LIMIT 1", pid, one=True)
    qs = json.loads(p["data"])
    out = []
    for i, item in enumerate(qs):
        mine = q("SELECT opt FROM answers WHERE run_id=? AND qidx=? AND user_id=?",
                 last["id"], i, g.user["id"], one=True) if last else None
        out.append(dict(text=item["text"], options=item["options"], correct=item["correct"],
                        counts=run_counts(last["id"], i, len(item["options"])) if last
                        else [0] * len(item["options"]), mine=mine["opt"] if mine else None))
    return render_template("poll_review.html", c=c, p=p, data=out)


# -------------------------------------------------------------- recordings
@app.route("/course/<int:cid>/summaries", methods=["GET", "POST"])
@login_required
def summaries(cid):
    c = course_access(cid)
    if request.method == "POST":
        if not is_teacher():
            abort(403)
        rec, tr = request.files.get("recording"), request.files.get("transcript")
        if not rec or not rec.filename:
            flash("לא נבחרה הקלטה", "error")
        else:
            tname = save_upload(tr) if tr and tr.filename else None
            run("INSERT INTO recordings(course_id,filename,transcript,uploaded) VALUES(?,?,?,?)",
                cid, save_upload(rec), tname, time.time())
        return redirect(url_for("summaries", cid=cid))
    recs = q("SELECT * FROM recordings WHERE course_id=? ORDER BY id DESC", cid)
    return render_template("summaries.html", c=c, recs=recs)


def get_rec(rid):
    r = q("SELECT * FROM recordings WHERE id=?", rid, one=True) or abort(404)
    return r, course_access(r["course_id"])


@app.route("/recording/<int:rid>/summary", methods=["POST"])
@login_required
def recording_summary(rid):
    r, c = get_rec(rid)
    if r["summary"] and not ai.is_failed_summary(r["summary"]):
        return jsonify(summary=r["summary"], saved=True)
    text = read_text(os.path.join(UPLOADS, r["transcript"])) if r["transcript"] else ""
    summary = ai.summarize_lecture(text)
    # the "no transcript" notice and AI-failure stand-ins are not cached as real summaries
    saved = bool(text.strip()) and not ai.is_failed_summary(summary)
    if saved:
        run("UPDATE recordings SET summary=? WHERE id=?", summary, rid)
    return jsonify(summary=summary, saved=saved)


@app.route("/recording/<int:rid>/summary.txt")
@login_required
def recording_summary_download(rid):
    r, c = get_rec(rid)
    return Response((r["summary"] or "").encode("utf-8"), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=summary_%d.txt" % rid})


@app.route("/recording/<int:rid>/delete", methods=["POST"])
@login_required
def recording_delete(rid):
    r, c = get_rec(rid)
    course_access(c["id"], teacher_only=True)
    run("DELETE FROM recordings WHERE id=?", rid)
    return redirect(url_for("summaries", cid=c["id"]))


# ---------------------------------------------------------------- homework
@app.route("/course/<int:cid>/homework", methods=["GET", "POST"])
@login_required
def homework(cid):
    c = course_access(cid)
    if request.method == "POST":
        if not is_teacher():
            abort(403)
        name = request.form.get("name", "").strip()
        due = parse_dt(request.form.get("due"), "%Y-%m-%dT%H:%M")
        if not name:
            flash("יש להזין שם למטלה", "error")
        elif q("SELECT 1 FROM boxes WHERE course_id=? AND lower(trim(name))=lower(?)",
               cid, name, one=True):
            flash("כבר קיימת מטלה בשם \"%s\" בקורס הזה" % name, "error")
        elif not due:
            flash("תאריך או שעה לא תקינים", "error")
        elif is_past(due):
            flash("לא ניתן לקבוע מועד הגשה שכבר עבר", "error")
        else:
            run("INSERT INTO boxes(course_id,name,due) VALUES(?,?,?)", cid, name, due.timestamp())
        return redirect(url_for("homework", cid=cid))
    boxes = q("SELECT * FROM boxes WHERE course_id=? ORDER BY id DESC", cid)
    return render_template("homework.html", c=c, boxes=boxes)


def get_box(bid):
    b = q("SELECT * FROM boxes WHERE id=?", bid, one=True) or abort(404)
    return b, course_access(b["course_id"])


@app.route("/box/<int:bid>/delete", methods=["POST"])
@login_required
def box_delete(bid):
    b, c = get_box(bid)
    course_access(c["id"], teacher_only=True)
    for s in q("SELECT filename FROM submissions WHERE box_id=?", bid):
        try:
            os.remove(os.path.join(UPLOADS, s["filename"]))
        except OSError:
            pass
    db().execute("DELETE FROM submissions WHERE box_id=?", (bid,))
    run("DELETE FROM boxes WHERE id=?", bid)
    flash("המטלה \"%s\" נמחקה" % b["name"], "ok")
    return redirect(url_for("homework", cid=c["id"]))


def late_label(box, ts):
    if ts <= box["due"]:
        return "המטלה הוגשה בזמן", False
    hours = max(1, -(-int(ts - box["due"]) // 3600))  # ceil, min 1
    return "המטלה הוגשה באיחור (%d שעות)" % hours, True


@app.route("/box/<int:bid>", methods=["GET", "POST"])
@login_required
def box(bid):
    b, c = get_box(bid)
    if request.method == "POST":
        if is_teacher():
            if "due" in request.form:  # "שינוי"
                due = parse_dt(request.form["due"], "%Y-%m-%dT%H:%M")
                if not due:
                    flash("תאריך או שעה לא תקינים", "error")
                elif is_past(due):
                    flash("לא ניתן לקבוע מועד הגשה שכבר עבר", "error")
                else:
                    run("UPDATE boxes SET due=? WHERE id=?", due.timestamp(), bid)
        else:
            f = request.files.get("work")
            if f and f.filename:
                name = save_upload(f)
                text = read_text(os.path.join(UPLOADS, name))
                db().execute("DELETE FROM submissions WHERE box_id=? AND user_id=?", (bid, g.user["id"]))
                run("INSERT INTO submissions(box_id,user_id,filename,orig,ts,text) VALUES(?,?,?,?,?,?)",
                    bid, g.user["id"], name, f.filename, time.time(), text)
                flash("העבודה הוגשה בהצלחה", "ok")
        return redirect(url_for("box", bid=bid))
    if is_teacher():
        subs = q("SELECT s.*, u.name FROM submissions s JOIN users u ON u.id=s.user_id "
                 "WHERE box_id=? ORDER BY s.ts", bid)
    else:
        subs = q("SELECT s.*, u.name FROM submissions s JOIN users u ON u.id=s.user_id "
                 "WHERE box_id=? AND user_id=?", bid, g.user["id"])
    items = [dict(s=s, label=late_label(b, s["ts"])) for s in subs]
    return render_template("box.html", c=c, b=b, items=items,
                           due_input=datetime.fromtimestamp(b["due"]).strftime("%Y-%m-%dT%H:%M"))


@app.route("/submission/<int:sid>")
@login_required
def submission(sid):
    s = q("SELECT * FROM submissions WHERE id=?", sid, one=True) or abort(404)
    b, c = get_box(s["box_id"])
    if not is_teacher() and s["user_id"] != g.user["id"]:
        abort(403)
    owner = q("SELECT name FROM users WHERE id=?", s["user_id"], one=True)["name"]
    return render_template("submission.html", s=s, b=b, c=c, owner=owner,
                           label=late_label(b, s["ts"]))


@app.route("/submission/<int:sid>/review", methods=["POST"])
@login_required
def submission_review(sid):
    s = q("SELECT * FROM submissions WHERE id=?", sid, one=True) or abort(404)
    b, c = get_box(s["box_id"])
    if not is_teacher() and s["user_id"] != g.user["id"]:
        abort(403)
    others = [("עבודה %d" % (i + 1), r["text"] or "") for i, r in enumerate(q(
        "SELECT text FROM submissions WHERE box_id=? AND id!=?", s["box_id"], sid))]
    return jsonify(review=ai.review_homework("", s["text"] or "", others))


# ------------------------------------------------- tab 6: participants/export
@app.route("/course/<int:cid>/more", methods=["GET", "POST"])
@login_required
def more(cid):
    c = course_access(cid)
    if request.method == "POST" and is_teacher() and request.form.get("body", "").strip():
        run("INSERT INTO announcements(course_id,body,ts) VALUES(?,?,?)", cid,
            request.form["body"].strip(), time.time())
        return redirect(url_for("more", cid=cid))
    ann = q("SELECT * FROM announcements WHERE course_id=? ORDER BY id DESC", cid)
    people = q("SELECT u.name, u.username FROM users u JOIN enroll e ON e.user_id=u.id "
               "WHERE e.course_id=? ORDER BY u.name", cid)
    return render_template("more.html", c=c, ann=ann, people=people)


@app.route("/course/<int:cid>/announcement/<int:aid>/delete", methods=["POST"])
@login_required
def announcement_delete(cid, aid):
    course_access(cid, teacher_only=True)
    run("DELETE FROM announcements WHERE id=? AND course_id=?", aid, cid)
    return redirect(url_for("more", cid=cid))


@app.route("/media/<name>")
@login_required
def media(name):
    # Only serve files that belong to a course the user can access.
    name = secure_filename(name)
    for table, cond in (("att_photos", "course_id"), ("recordings", "course_id")):
        row = q("SELECT %s cid FROM %s WHERE filename=?" % (cond, table), name, one=True)
        if row:
            course_access(row["cid"])
            return send_from_directory(UPLOADS, name)
    row = q("SELECT b.course_id cid, s.user_id uid FROM submissions s JOIN boxes b ON b.id=s.box_id "
            "WHERE s.filename=?", name, one=True)
    if row:
        course_access(row["cid"])
        if not is_teacher() and row["uid"] != g.user["id"]:
            abort(403)
        return send_from_directory(UPLOADS, name)
    abort(404)


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(os.path.join(BASE, "static"), "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.context_processor
def inject():
    return dict(ai_on=ai.ai_enabled())


init_db()

if __name__ == "__main__":
    ctx = "adhoc" if os.environ.get("HTTPS") == "1" else None  # needs: pip install cryptography
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, ssl_context=ctx)
