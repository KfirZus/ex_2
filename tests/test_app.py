import os
import sqlite3
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp()
os.environ["SC_DATA"] = TMP
os.makedirs(os.path.join(TMP, "uploads"), exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai  # noqa: E402
import app as m  # noqa: E402

OFF_WIFI = {"REMOTE_ADDR": "8.8.8.8"}


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


class Base(unittest.TestCase):
    def setUp(self):
        for t in ("users", "courses", "enroll", "att_sessions", "att_records", "att_photos",
                  "lessons", "comments", "polls", "runs", "answers", "recordings", "boxes",
                  "submissions", "announcements"):
            sql("DELETE FROM " + t)
        self.t = m.app.test_client()
        self.s = m.app.test_client()
        self.reg(self.t, "teach", "Teacher", "teacher", m.TEACHER_CODE)
        self.reg(self.s, "stud", "Student", "student")
        r = self.t.post("/course/new", data=dict(name="Course", day="ראשון", time="10:00"))
        self.cid = int(r.headers["Location"].rsplit("/", 1)[1])
        code = one("SELECT code FROM courses")[0]
        self.s.post("/join", data={"code": code})

    def reg(self, client, user, name, role, code=""):
        return client.post("/register", data=dict(username=user, password="pw", name=name,
                                                  role=role, teacher_code=code))


class AccessAndJoin(Base):
    def test_teacher_code_required(self):
        c = m.app.test_client()
        c.post("/register", data=dict(username="x", password="p", name="X", role="teacher",
                                      teacher_code="wrong"))
        self.assertIsNone(one("SELECT 1 FROM users WHERE username='x'"))

    def test_join_via_code_enrolls_student(self):
        self.assertEqual(one("SELECT COUNT(*) FROM enroll")[0], 1)
        self.assertEqual(self.s.get("/course/%d" % self.cid).status_code, 200)

    def test_outsider_forbidden(self):
        o = m.app.test_client()
        self.reg(o, "other", "Other", "student")
        self.assertEqual(o.get("/course/%d" % self.cid).status_code, 403)
        self.assertEqual(o.get("/course/%d/attendance" % self.cid).status_code, 403)

    def test_student_cannot_use_teacher_actions(self):
        self.assertEqual(self.s.post("/course/%d/attendance/start" % self.cid).status_code, 403)
        self.assertEqual(self.s.get("/course/%d/add-people" % self.cid).status_code, 403)

    def test_every_page_renders(self):
        c = self.cid
        for client in (self.t, self.s):
            for p in ("", "/attendance", "/notes", "/polls", "/summaries", "/homework", "/more"):
                self.assertEqual(client.get("/course/%d%s" % (c, p)).status_code, 200, p)
        self.assertEqual(self.t.get("/course/%d/add-people" % c).status_code, 200)
        self.assertEqual(self.t.get("/course/%d/polls/new" % c).status_code, 200)
        self.assertEqual(self.s.get("/join").status_code, 200)


class Attendance(Base):
    def url(self, p):
        return "/course/%d/attendance%s" % (self.cid, p)

    def test_checkin_only_on_wifi_and_inside_window(self):
        self.s.post(self.url("/checkin"))  # no active session
        self.assertEqual(one("SELECT COUNT(*) FROM att_records")[0], 0)
        self.t.post(self.url("/start"))
        self.s.post(self.url("/checkin"), environ_base=OFF_WIFI)
        self.assertEqual(one("SELECT COUNT(*) FROM att_records")[0], 0)
        self.assertTrue(self.s.post(self.url("/checkin")).get_json()["ok"])
        self.assertEqual(one("SELECT COUNT(*) FROM att_records")[0], 1)

    def test_locked_after_ten_minutes(self):
        self.t.post(self.url("/start"))
        s_id, started, ends = one("SELECT id,started,ends FROM att_sessions")
        self.assertAlmostEqual(ends - started, 600, delta=1)
        sql("UPDATE att_sessions SET ends=?", time.time() - 1)
        self.s.post(self.url("/checkin"))
        self.assertEqual(one("SELECT COUNT(*) FROM att_records")[0], 0)

    def test_single_active_session(self):
        self.t.post(self.url("/start"))
        self.t.post(self.url("/start"))
        self.assertEqual(one("SELECT COUNT(*) FROM att_sessions")[0], 1)

    def test_student_record_text_and_export(self):
        self.t.post(self.url("/start"))
        self.s.post(self.url("/checkin"))
        sql("UPDATE att_sessions SET ends=?", time.time() - 1)
        self.t.post(self.url("/start"))  # second session, student absent
        sql("UPDATE att_sessions SET ends=? WHERE id=2", time.time() - 1)
        html = self.s.get(self.url("")).get_data(as_text=True)
        self.assertIn("היית נוכח בתאריך", html)
        self.assertIn("לא היית נוכח בתאריך", html)
        csv_text = self.t.get(self.url("/export.csv")).get_data(as_text=True)
        self.assertIn("Student", csv_text)
        self.assertIn("V", csv_text)
        self.assertIn("X", csv_text)
        self.assertIn("50%", csv_text)
        self.assertEqual(self.s.get(self.url("/export.csv")).status_code, 403)

    def test_photo_upload_and_delete(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), "white").save(buf, "JPEG")
        buf.seek(0)
        self.t.post(self.url("/photo"), data={"photo": (buf, "class.jpg")},
                    content_type="multipart/form-data")
        self.assertEqual(one("SELECT COUNT(*) FROM att_photos")[0], 1)
        name = one("SELECT filename FROM att_photos")[0]
        self.assertEqual(self.s.get("/media/" + name).status_code, 200)
        pid = one("SELECT id FROM att_photos")[0]
        self.t.post(self.url("/photo/%d/delete" % pid))
        self.assertEqual(one("SELECT COUNT(*) FROM att_photos")[0], 0)


class Notes(Base):
    def test_comments_gated_by_start_and_limited(self):
        self.t.post("/course/%d/notes" % self.cid, data=dict(topic="T", date="2099-01-01", start="09:00"))
        lid = one("SELECT id FROM lessons")[0]
        r = self.s.post("/lesson/%d/comments" % lid, json={"body": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.t.post("/lesson/%d/comments" % lid, json={"body": "teacher"}).status_code, 200)
        sql("UPDATE lessons SET date='2000-01-01'")
        self.s.post("/lesson/%d/comments" % lid, json={"body": "x" * 500, "anon": True})
        msgs = self.s.get("/lesson/%d/comments" % lid).get_json()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["label"], "student")
        self.assertEqual(len(msgs[1]["body"]), 200)
        self.assertEqual(msgs[0]["label"], "Teacher")

    def test_confirmation_message(self):
        r = self.t.post("/course/%d/notes" % self.cid, data=dict(topic="Sorting", date="2030-05-01",
                                                                 start="09:30"), follow_redirects=True)
        self.assertIn("קבעת שיעור לתאריך 01/05/2030 בנושא Sorting האפשרות להגיב תתחיל בשעה 09:30",
                      r.get_data(as_text=True))


class Polls(Base):
    def setUp(self):
        super().setUp()
        r = self.t.post("/course/%d/polls/save" % self.cid, json={"title": "P", "questions": [
            {"text": "Q1", "options": [{"text": "a"}, {"text": "b", "correct": True}]},
            {"text": "Q2", "options": [{"text": "c", "correct": True}, {"text": "d"}]}]})
        self.assertTrue(r.get_json()["ok"])
        self.pid = one("SELECT id FROM polls")[0]
        self.t.post("/course/%d/polls/%d/start" % (self.cid, self.pid))
        self.rid = one("SELECT id FROM runs")[0]

    def at(self, elapsed):
        sql("UPDATE runs SET started=?", time.time() - elapsed)
        return self.s.get("/poll-run/%d/state" % self.rid).get_json()

    def test_phases(self):
        self.assertTrue(self.at(-5)["waiting"])
        st = self.at(3)
        self.assertEqual((st["idx"], st["phase"]), (0, "answer"))
        self.assertNotIn("correct", st)
        st = self.at(11)
        self.assertEqual((st["idx"], st["phase"], st["correct"]), (0, "reveal", 1))
        self.assertEqual(self.at(16)["idx"], 1)
        self.assertTrue(self.at(31)["finished"])

    def test_answer_change_and_lock(self):
        self.at(2)
        url = "/poll-run/%d/answer" % self.rid
        self.s.post(url, json={"opt": 0})
        self.s.post(url, json={"opt": 1})  # change while timer runs
        self.assertEqual(one("SELECT opt FROM answers")[0], 1)
        self.at(12)  # reveal phase: answers rejected
        self.assertEqual(self.s.post(url, json={"opt": 0}).status_code, 400)
        st = self.at(12)
        self.assertEqual(st["counts"], [0, 1])

    def test_invalid_poll_rejected_and_delete(self):
        bad = self.t.post("/course/%d/polls/save" % self.cid,
                          json={"questions": [{"text": "x", "options": [{"text": "only"}]}]})
        self.assertEqual(bad.status_code, 400)
        self.t.post("/course/%d/polls/%d/delete" % (self.cid, self.pid))
        self.assertEqual(one("SELECT COUNT(*) FROM polls")[0], 0)

    def test_review_page(self):
        self.at(40)
        self.assertEqual(self.s.get("/poll/%d/review" % self.pid).status_code, 200)


class Homework(Base):
    def submit(self, client, text, bid):
        import io
        return client.post("/box/%d" % bid, data={"work": (io.BytesIO(text.encode()), "w.txt")},
                           content_type="multipart/form-data")

    def test_late_labels_and_review(self):
        due = "2099-01-01T10:00"
        self.t.post("/course/%d/homework" % self.cid, data=dict(name="Task 1", due=due))
        bid = one("SELECT id FROM boxes")[0]
        self.submit(self.s, "binary search trees insertion deletion " * 20, bid)
        html = self.t.get("/box/%d" % bid).get_data(as_text=True)
        self.assertIn("המטלה הוגשה בזמן", html)
        sql("UPDATE boxes SET due=?", time.time() - 3600 * 2 - 60)
        html = self.t.get("/box/%d" % bid).get_data(as_text=True)
        self.assertIn("באיחור (3 שעות)", html)
        # second student for peer comparison
        s2 = m.app.test_client()
        self.reg(s2, "stud2", "S2", "student")
        s2.post("/join", data={"code": one("SELECT code FROM courses")[0]})
        self.submit(s2, "binary search trees insertion deletion " * 20, bid)
        sid = one("SELECT id FROM submissions WHERE user_id=(SELECT id FROM users WHERE username='stud')")[0]
        rev = self.s.post("/submission/%d/review" % sid).get_json()["review"]
        self.assertIn("1 עבודות אחרות", rev)
        sid2 = one("SELECT id FROM submissions WHERE id!=?", sid)[0]
        self.assertEqual(self.s.get("/submission/%d" % sid2).status_code, 403)
        self.assertEqual(self.t.get("/submission/%d" % sid2).status_code, 200)

    def test_teacher_edit_deadline(self):
        self.t.post("/course/%d/homework" % self.cid, data=dict(name="T", due="2099-01-01T10:00"))
        bid = one("SELECT id FROM boxes")[0]
        self.t.post("/box/%d" % bid, data=dict(due="2099-02-02T12:00"))
        self.assertIn("02/02/2099 12:00", self.t.get("/box/%d" % bid).get_data(as_text=True))


class Recordings(Base):
    def test_summary_with_transcript(self):
        import io
        text = ("Today we discussed requirements engineering and use cases in detail. " * 3 +
                "A class diagram shows static structure of the system and its relations. " * 3 +
                "Sequence diagrams show the order of messages between objects over time. " * 3 +
                "Testing strategy should cover every class and every important behaviour. " * 3 +
                "The SRS document lists functional and non functional requirements clearly. " * 3 +
                "Finally we reviewed the demo plan and the schedule for the coming weeks. " * 3 +
                "Students asked many questions about the homework deadline and format. " * 3 +
                "We closed with a short summary of the main design patterns discussed. " * 3)
        self.t.post("/course/%d/summaries" % self.cid, data={
            "recording": (io.BytesIO(b"\0" * 10), "lec.mp4"),
            "transcript": (io.BytesIO(text.encode()), "t.txt")}, content_type="multipart/form-data")
        rid = one("SELECT id FROM recordings")[0]
        out = self.s.post("/recording/%d/summary" % rid).get_json()["summary"]
        self.assertTrue(len(out) > 50)
        dl = self.s.get("/recording/%d/summary.txt" % rid)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(self.s.post("/recording/%d/delete" % rid).status_code, 403)
        self.t.post("/recording/%d/delete" % rid)
        self.assertEqual(one("SELECT COUNT(*) FROM recordings")[0], 0)

    def test_no_transcript_message(self):
        self.assertIn("תמלול", ai.summarize_lecture(""))


if __name__ == "__main__":
    unittest.main()
