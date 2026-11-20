"""Shared fixtures for the STP / STD test suite.

Every test module imports this file first. It points the app at a throw-away data folder
(SC_DATA) before `app` is imported, disables the real AI so results are deterministic, and
offers a Fixture base class that builds the same baseline data the STD describes:
teacher + student1..student3 enrolled in course 1, a second teacher with course 2, and an
`outsider` student who is not enrolled anywhere.
"""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

if "app" not in sys.modules:
    _tmp = tempfile.mkdtemp(prefix="sc_tests_")
    os.environ["SC_DATA"] = _tmp
    os.makedirs(os.path.join(_tmp, "uploads"), exist_ok=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai  # noqa: E402
import app as m  # noqa: E402

m.app.logger.disabled = True  # expected-failure cases hit 500s; keep their tracebacks out of the output
ai.API_KEY = None  # AI-dependent cases use the local fallbacks; real-key runs are manual (STD P-09 step 5)

DEFAULT_NETS = [m.ipaddress.ip_network(n) for n in
                ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
m.ALLOWED_NETS[:] = DEFAULT_NETS  # ignore an ALLOWED_NETWORKS left in the developer's shell

TABLES = ("users", "courses", "enroll", "att_sessions", "att_records", "att_photos", "lessons",
          "comments", "polls", "runs", "answers", "recordings", "boxes", "submissions",
          "announcements")

OFF_WIFI = {"REMOTE_ADDR": "8.8.8.8"}
PW = "1234"


def sql(query, *args):
    c = sqlite3.connect(m.DB_PATH)
    c.execute(query, args)
    c.commit()
    c.close()


def one(query, *args):
    c = sqlite3.connect(m.DB_PATH)
    row = c.execute(query, args).fetchone()
    c.close()
    return row


def count(table, where="1=1", *args):
    return one("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), *args)[0]


def text_file(content, name="w.txt"):
    return io.BytesIO(content.encode("utf-8")), name


def jpeg(name="class.jpg"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, "JPEG")
    buf.seek(0)
    return buf, name


def page_json(html, var="D"):
    """Extracts `const D = {...};` embedded by a template with |tojson."""
    mt = re.search(r"const %s = (.*?);\n" % var, html, re.S)
    return json.loads(mt.group(1))


class Fixture(unittest.TestCase):
    """Fresh database + logged-in clients for every test."""

    STUDENTS = 3

    def setUp(self):
        for t in TABLES:
            sql("DELETE FROM " + t)
        m.ALLOWED_NETS[:] = DEFAULT_NETS
        self.t = self.register("teacher", "המרצה", "teacher", m.TEACHER_CODE)
        self.cid = self.new_course(self.t, "מבוא להנדסת תוכנה")
        self.code = one("SELECT code FROM courses WHERE id=?", self.cid)[0]
        self.students = []
        for i in range(1, self.STUDENTS + 1):
            c = self.register("student%d" % i, "תלמיד %d" % i)
            c.post("/join", data={"code": self.code})
            self.students.append(c)
        self.s1 = self.students[0] if self.students else None

    # ------------------------------------------------------------ helpers
    def client(self):
        return m.app.test_client()

    def register(self, username, name, role="student", code=""):
        c = self.client()
        c.post("/register", data=dict(username=username, password=PW, name=name, role=role,
                                      teacher_code=code))
        return c

    def login(self, username, password=PW):
        c = self.client()
        c.post("/login", data=dict(username=username, password=password))
        return c

    def uid(self, username):
        return one("SELECT id FROM users WHERE username=?", username)[0]

    def new_course(self, teacher, name, day="ראשון", hour="10:00"):
        r = teacher.post("/course/new", data=dict(name=name, day=day, time=hour))
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def att(self, p="", cid=None):
        return "/course/%d/attendance%s" % (cid or self.cid, p)

    def end_attendance(self):
        sql("UPDATE att_sessions SET ends=?", time.time() - 1)

    def save_poll(self, questions, title="P", cid=None, teacher=None):
        r = (teacher or self.t).post("/course/%d/polls/save" % (cid or self.cid),
                                     json={"title": title, "questions": questions})
        return r

    def start_poll(self, pid):
        self.t.post("/course/%d/polls/%d/start" % (self.cid, pid))
        return one("SELECT MAX(id) FROM runs")[0]

    def at(self, rid, elapsed, client=None):
        """Moves the poll run so that `elapsed` seconds have passed, returns the state JSON."""
        sql("UPDATE runs SET started=? WHERE id=?", time.time() - elapsed, rid)
        return (client or self.s1).get("/poll-run/%d/state" % rid).get_json()

    def new_lesson(self, topic="בדיקה", cid=None, past=True):
        """Lessons can only be scheduled in the future; `past` then moves the start into the
        past so comments are already open."""
        self.t.post("/course/%d/notes" % (cid or self.cid),
                    data=dict(topic=topic, date="2099-01-01", start="09:00"))
        lid = one("SELECT MAX(id) FROM lessons")[0]
        if past:
            sql("UPDATE lessons SET date='2000-01-01' WHERE id=?", lid)
        return lid

    def new_box(self, name="מטלה 1", due="2099-01-01T10:00", past=False):
        """Deadlines can only be set in the future; `past` moves it 2 hours into the past."""
        self.t.post("/course/%d/homework" % self.cid, data=dict(name=name, due=due))
        bid = one("SELECT MAX(id) FROM boxes")[0]
        if past:
            sql("UPDATE boxes SET due=? WHERE id=?", time.time() - 7200, bid)
        return bid

    def submit(self, client, bid, content, name="w.txt"):
        return client.post("/box/%d" % bid, data={"work": text_file(content, name)},
                           content_type="multipart/form-data")

    def status(self, client, path, method="GET", **kw):
        return getattr(client, method.lower())(path, **kw).status_code
