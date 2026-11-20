"""STP section 6 / STD section 3 - permission and security cases P-01 .. P-18.

The STD runs P-01, P-03..P-06, P-09 and P-10 by hand with curl.exe; these tests are the
automated version of exactly the same steps and expected results, on the STD baseline data
(lesson "בדיקה", one poll run, box "מטלה 1", one recording, three submissions).
"""
import os
import subprocess
import sys
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import OFF_WIFI, ROOT, Fixture, count, jpeg, m, one, text_file  # noqa: E402

Q1 = [{"text": "Q", "options": [{"text": "a"}, {"text": "b", "correct": True}]}]


class Baseline(Fixture):
    """STD section 3 "baseline data": every object has id 1 in course 1."""

    def setUp(self):
        super().setUp()
        self.lid = self.new_lesson("בדיקה")
        self.save_poll(Q1)
        self.rid = self.start_poll(one("SELECT id FROM polls")[0])
        self.bid = self.new_box()
        self.t.post("/course/%d/summaries" % self.cid,
                    data={"recording": text_file("rec", "lec.mp4")}, content_type="multipart/form-data")
        for i, c in enumerate(self.students):
            self.submit(c, self.bid, "homework text number %d about trees" % i)
        self.sub = {i + 1: one("SELECT id, filename FROM submissions WHERE user_id=?",
                               self.uid("student%d" % (i + 1))) for i in range(3)}
        self.pid = one("SELECT id FROM polls")[0]
        self.rec = one("SELECT id FROM recordings")[0]
        self.t2 = self.register("teacher2", "מורה 2", "teacher", m.TEACHER_CODE)
        self.cid2 = self.new_course(self.t2, "קורס אחר")
        self.o = self.register("outsider", "זר")


class Anonymous(Baseline):
    def test_P01_every_route_redirects_to_login(self):
        c = self.client()
        paths = ["/courses", "/course/%d" % self.cid, self.att(), self.att("/export.csv"),
                 "/course/%d/notes" % self.cid, "/lesson/%d" % self.lid, "/course/%d/polls" % self.cid,
                 "/poll/%d/review" % self.pid, "/course/%d/summaries" % self.cid,
                 "/course/%d/homework" % self.cid, "/box/%d" % self.bid,
                 "/submission/%d" % self.sub[1][0], "/course/%d/more" % self.cid, "/join", "/media/x.jpg"]
        self.assertEqual(len(paths), 15)
        for p in paths:
            r = c.get(p)
            self.assertEqual(r.status_code, 302, p)
            self.assertEqual(urlsplit(r.headers["Location"]).path, "/login", p)
        for p in ("/login", "/register", "/static/style.css", "/sw.js"):
            self.assertEqual(c.get(p).status_code, 200, p)


class Registration(Fixture):
    STUDENTS = 0

    def test_P02_teacher_code_required(self):
        for code in ("wrong", ""):
            c = self.client()
            c.post("/register", data=dict(username="fake", password="p", name="F", role="teacher",
                                          teacher_code=code))
            self.assertIsNone(one("SELECT 1 FROM users WHERE username='fake'"))


class Roles(Baseline):
    def test_P03_student_cannot_do_teacher_actions(self):
        sessions, polls, courses = count("att_sessions"), count("polls"), count("courses")
        s = self.s1
        codes = [s.post(self.att("/start")).status_code,
                 s.get(self.att("/export.csv")).status_code,
                 s.get("/course/%d/add-people" % self.cid).status_code,
                 s.get("/course/%d/polls/new" % self.cid).status_code,
                 s.post("/course/%d/polls/save" % self.cid, json={}).status_code,
                 s.post(self.att("/photo"), data={"photo": jpeg()}, content_type="multipart/form-data").status_code,
                 s.post("/course/new", data=dict(name="x", day="x", time="x")).status_code]
        self.assertEqual(codes, [403] * 7)
        self.assertEqual((count("att_sessions"), count("polls"), count("courses")), (sessions, polls, courses))

    def test_P04_outsider_gets_403_and_no_leak(self):
        o = self.o
        gets = ["/course/%d" % self.cid, self.att(), "/course/%d/notes" % self.cid,
                "/course/%d/polls" % self.cid, "/course/%d/summaries" % self.cid,
                "/course/%d/homework" % self.cid, "/course/%d/more" % self.cid, "/lesson/%d" % self.lid,
                "/lesson/%d/comments" % self.lid, "/poll-run/%d" % self.rid, "/poll-run/%d/state" % self.rid,
                "/poll/%d/review" % self.pid, "/box/%d" % self.bid, "/submission/%d" % self.sub[1][0],
                "/media/" + self.sub[1][1]]
        for p in gets:
            r = o.get(p)
            self.assertEqual(r.status_code, 403, p)
            body = r.get_data(as_text=True)
            self.assertNotIn("בדיקה", body, p)
            self.assertNotIn("תלמיד 1", body, p)
        posts = [("/poll-run/%d/answer" % self.rid, {"json": {"opt": 0}}),
                 ("/submission/%d/review" % self.sub[1][0], {}),
                 ("/recording/%d/summary" % self.rec, {}),
                 (self.att("/checkin"), {})]
        for p, kw in posts:
            self.assertEqual(o.post(p, **kw).status_code, 403, p)
        self.assertEqual((count("att_records"), count("answers")), (0, 0))

    def test_P05_other_teacher_isolated(self):
        before = count("att_sessions")
        for p in ("/course/%d" % self.cid, "/course/%d/add-people" % self.cid,
                  self.att("/export.csv"), "/course/%d/polls" % self.cid):
            self.assertEqual(self.t2.get(p).status_code, 403, p)
        self.assertEqual(self.t2.post(self.att("/start")).status_code, 403)
        self.assertEqual(self.t2.get("/course/%d" % self.cid2).status_code, 200)
        self.assertEqual(self.t.get("/course/%d" % self.cid2).status_code, 403)
        self.assertEqual(count("att_sessions"), before)

    def test_P06_peer_submission_media(self):
        url = "/media/" + self.sub[1][1]
        self.assertEqual(self.s1.get(url).status_code, 200)
        self.assertEqual(self.t.get(url).status_code, 200)
        self.assertEqual(self.students[1].get(url).status_code, 403)
        self.assertEqual(self.o.get(url).status_code, 403)
        self.assertEqual(self.students[1].get("/submission/%d" % self.sub[1][0]).status_code, 403)

    def test_P07_media_guessing_and_traversal(self):
        for name in ("nothere.txt", "..%2fclassroom.db", "..%2f..%2fapp.py", "classroom.db"):
            self.assertEqual(self.t.get("/media/" + name).status_code, 404, name)

    def test_P08_anonymous_comment_hides_identity(self):
        self.s1.post("/lesson/%d/comments" % self.lid, json={"body": "שאלה", "anon": True})
        self.students[1].post("/lesson/%d/comments" % self.lid, json={"body": "עוד", "anon": True})
        raw = self.t.get("/lesson/%d/comments" % self.lid).get_data(as_text=True)
        msgs = self.t.get("/lesson/%d/comments" % self.lid).get_json()
        self.assertEqual([x["label"] for x in msgs], ["student", "student"])
        for leak in ("user_id", "student1", "student2", "תלמיד 1", "תלמיד 2"):
            self.assertNotIn(leak, raw)

    def test_P09_ai_review_permissions_and_anonymity(self):
        s1_sub = self.sub[1][0]
        self.assertEqual(self.students[1].post("/submission/%d/review" % s1_sub).status_code, 403)
        rev = self.s1.post("/submission/%d/review" % s1_sub).get_json()["review"]
        self.assertTrue(rev)
        for name in ("student2", "student3", "תלמיד"):
            self.assertNotIn(name, rev)
        html = self.s1.get("/box/%d" % self.bid).get_data(as_text=True)
        self.assertEqual(html.count("/submission/"), 1)

    @unittest.expectedFailure  # STD/STP: expected to fail today (defect D-01, X-Forwarded-For)
    def test_P10_forwarded_for_does_not_bypass_wifi(self):
        m.ALLOWED_NETS[:] = [m.ipaddress.ip_network("192.168.1.0/24")]
        self.t.post(self.att("/start"))
        self.assertFalse(self.s1.post(self.att("/checkin")).get_json()["ok"])
        r = self.s1.post(self.att("/checkin"), headers={"X-Forwarded-For": "192.168.1.50"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(count("att_records"), 0)


class Injection(Baseline):
    XSS = "<script>alert(1)</script>"

    @unittest.expectedFailure  # STP: expected to fail today (uploaded HTML served inline)
    def test_P11_uploaded_html_not_rendered_inline(self):
        self.submit(self.s1, self.bid, "<html><body>" + self.XSS + "</body></html>", "evil.html")
        name = one("SELECT filename FROM submissions WHERE user_id=?", self.uid("student1"))[0]
        r = self.t.get("/media/" + name)
        inline_html = r.mimetype in ("text/html", "image/svg+xml")
        self.assertTrue("attachment" in r.headers.get("Content-Disposition", "") or not inline_html)

    def test_P12_markup_is_escaped_everywhere(self):
        x = self.XSS
        cid = self.new_course(self.t, x)
        self.new_lesson(x, cid=cid)
        self.t.post("/course/%d/more" % cid, data=dict(body=x))
        self.save_poll([{"text": x, "options": [{"text": x}, {"text": "b"}]}], title=x, cid=cid)
        self.register("evil", x).post("/join", data={"code": one("SELECT code FROM courses WHERE id=?", cid)[0]})
        pid = one("SELECT MAX(id) FROM polls")[0]
        for p in ("/courses", "/course/%d" % cid, "/course/%d/notes" % cid, "/course/%d/more" % cid,
                  "/course/%d/polls" % cid, "/poll/%d/review" % pid,
                  "/lesson/%d" % one("SELECT MAX(id) FROM lessons")[0]):
            html = self.t.get(p).get_data(as_text=True)
            self.assertNotIn(x, html, p)

    def test_P13_sql_injection_has_no_effect(self):
        c = self.client()
        r = c.post("/login", data=dict(username="' OR 1=1 --", password="' OR 1=1 --"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/courses").status_code, 302)
        self.o.post("/join", data={"code": "' OR '1'='1"})
        self.assertIsNone(one("SELECT 1 FROM enroll WHERE user_id=?", self.uid("outsider")))
        self.s1.post("/lesson/%d/comments" % self.lid, json={"body": "x'); DROP TABLE users; --"})
        self.assertGreater(count("users"), 0)


class Hardening(Baseline):
    def import_app_stderr(self, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "TEACHER_CODE")}
        e.update(SC_DATA=m.DATA, **env)
        out = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=e,
                             capture_output=True, text=True, timeout=60)
        return (out.stdout + out.stderr).lower()

    @unittest.expectedFailure  # STP: expected to fail today (no warning for default SECRET_KEY)
    def test_P14_default_secret_key_warns(self):
        self.assertIn("secret_key", self.import_app_stderr())

    @unittest.expectedFailure  # STP: expected to fail today (no warning for default TEACHER_CODE)
    def test_P15_default_teacher_code_warns(self):
        self.assertIn("teacher_code", self.import_app_stderr())

    @unittest.expectedFailure  # STP: expected to fail today (no CSRF protection)
    def test_P16_cross_site_post_rejected(self):
        hdr = {"Origin": "http://evil.example", "Referer": "http://evil.example/x"}
        self.t.post(self.att("/start"), headers=hdr)
        self.t.post("/course/%d/polls/%d/delete" % (self.cid, self.pid), headers=hdr)
        self.assertEqual((count("att_sessions"), count("polls")), (0, 1))

    @unittest.expectedFailure  # STP: expected to fail today (no login rate limit)
    def test_P17_login_rate_limited(self):
        c = self.client()
        for _ in range(30):
            c.post("/login", data=dict(username="student1", password="wrong"))
        c.post("/login", data=dict(username="student1", password="1234"))
        self.assertEqual(c.get("/courses").status_code, 302)

    def test_P18_oversize_upload_rejected_with_413(self):
        old = m.app.config["MAX_CONTENT_LENGTH"]
        m.app.config["MAX_CONTENT_LENGTH"] = 1024
        try:
            r = self.s1.post("/box/%d" % self.bid, data={"work": text_file("x" * 4096)},
                             content_type="multipart/form-data")
        finally:
            m.app.config["MAX_CONTENT_LENGTH"] = old
        self.assertEqual(r.status_code, 413)
        self.assertEqual(old, 500 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
