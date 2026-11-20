"""STP section 5 - business-logic tests (the cases marked "new" or "partly existing").

L-01..L-05, L-07, L-11..L-13, L-15, L-16 and L-20 are already covered by tests/test_app.py.
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, Fixture, ai, count, jpeg, m, one, page_json, sql, text_file  # noqa: E402

Q4 = [{"text": "Q", "options": [{"text": "a", "correct": True}, {"text": "b"}, {"text": "c"},
                                {"text": "d"}]}]


class Attendance(Fixture):
    def test_L06_double_checkin_single_record(self):
        self.t.post(self.att("/start"))
        for _ in range(3):
            self.assertTrue(self.s1.post(self.att("/checkin")).get_json()["ok"])
        self.assertEqual(count("att_records"), 1)

    def test_L08_photo_registered_count_is_live(self):
        self.t.post(self.att("/start"))
        self.s1.post(self.att("/checkin"))
        self.t.post(self.att("/photo"), data={"photo": jpeg()}, content_type="multipart/form-data")
        self.students[1].post(self.att("/checkin"))
        html = self.t.get(self.att()).get_data(as_text=True)
        self.assertIn("כמות תלמידים שנרשמו בנוכחות:</b> 2", html)
        self.assertIn("לא זמין (נדרש מפתח AI)", html)

    def test_L09_export_has_bom_and_percent(self):
        self.t.post(self.att("/start"))
        self.s1.post(self.att("/checkin"))
        self.end_attendance()
        self.t.post(self.att("/start"))
        self.end_attendance()
        raw = self.t.get(self.att("/export.csv")).get_data(as_text=True)
        self.assertTrue(raw.startswith("﻿"))
        row = [l for l in raw.splitlines() if l.startswith("תלמיד 1,")][0]
        self.assertEqual(row.split(",")[2:], ["V", "X", "1", "50%"])

    def test_L10_export_without_sessions_or_students(self):
        r = self.t.get(self.att("/export.csv"))
        self.assertEqual(r.status_code, 200)
        lines = r.get_data(as_text=True).lstrip("﻿").splitlines()
        self.assertEqual(lines[0], 'שם,שם משתמש,"סה""כ נוכחות",אחוז')
        self.assertTrue(all(l.endswith(",0,0%") for l in lines[1:]))
        empty = self.new_course(self.t, "ריק")
        r = self.t.get(self.att("/export.csv", cid=empty))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_data(as_text=True).splitlines()), 1)


class Notes(Fixture):
    def test_L14_special_chars_kept_as_text(self):
        self.t.post("/course/%d/notes" % self.cid, data=dict(topic="T", date="2000-01-01", start="09:00"))
        lid = one("SELECT id FROM lessons")[0]
        body = "<img src=x onerror=alert(1)> & שלום 😀"
        self.s1.post("/lesson/%d/comments" % lid, json={"body": body})
        self.assertEqual(self.s1.get("/lesson/%d/comments" % lid).get_json()[0]["body"], body)
        with open(os.path.join(ROOT, "templates", "lesson.html"), encoding="utf-8") as f:
            self.assertNotIn("innerHTML", f.read())  # client renders with textContent only


class Polls(Fixture):
    STUDENTS = 4

    def test_L17_reveal_counts(self):
        self.save_poll(Q4)
        rid = self.start_poll(one("SELECT id FROM polls")[0])
        self.at(rid, 2)
        for c, opt in zip(self.students, (0, 0, 0, 1)):
            self.assertEqual(c.post("/poll-run/%d/answer" % rid, json={"opt": opt}).status_code, 200)
        st = self.at(rid, 11)
        self.assertEqual((st["phase"], st["counts"], st["correct"]), ("reveal", [3, 1, 0, 0], 0))

    def test_L15_correct_hidden_from_student_until_reveal(self):
        self.save_poll(Q4)
        rid = self.start_poll(one("SELECT id FROM polls")[0])
        st = self.at(rid, 3)
        self.assertNotIn("correct", st)
        self.assertNotIn("counts", st)

    def test_L18_review_shows_last_run_only(self):
        self.save_poll(Q4)
        pid = one("SELECT id FROM polls")[0]
        rid1 = self.start_poll(pid)
        self.at(rid1, 2)
        self.s1.post("/poll-run/%d/answer" % rid1, json={"opt": 2})
        self.at(rid1, 40)
        rid2 = self.start_poll(pid)
        self.at(rid2, 2)
        self.s1.post("/poll-run/%d/answer" % rid2, json={"opt": 0})
        self.at(rid2, 40)
        data = page_json(self.s1.get("/poll/%d/review" % pid).get_data(as_text=True))
        self.assertEqual((data[0]["mine"], data[0]["counts"], data[0]["correct"]), (0, [1, 0, 0, 0], 0))
        other = page_json(self.students[1].get("/poll/%d/review" % pid).get_data(as_text=True))
        self.assertIsNone(other[0]["mine"])

    @unittest.expectedFailure  # STP: expected to fail today (state of a deleted poll -> 500)
    def test_L19_delete_poll_with_live_run(self):
        self.save_poll(Q4)
        pid = one("SELECT id FROM polls")[0]
        rid = self.start_poll(pid)
        self.at(rid, 2)
        self.s1.post("/poll-run/%d/answer" % rid, json={"opt": 1})
        self.t.post("/course/%d/polls/%d/delete" % (self.cid, pid))
        self.assertEqual(count("polls"), 0)
        self.assertLess(self.s1.get("/poll-run/%d/state" % rid).status_code, 500)
        self.assertLess(self.s1.get("/course/%d/polls" % self.cid).status_code, 500)


class Homework(Fixture):
    def test_L21_resubmission_replaces_old(self):
        bid = self.new_box()
        self.submit(self.s1, bid, "first version", "v1.txt")
        self.submit(self.s1, bid, "second version", "v2.txt")
        self.assertEqual(count("submissions"), 1)
        self.assertEqual(one("SELECT orig, text FROM submissions"), ("v2.txt", "second version"))

    def test_L22_deadline_change_updates_label(self):
        bid = self.new_box(due="2000-01-01T10:00")
        self.submit(self.s1, bid, "work")
        self.assertIn("באיחור", self.t.get("/box/%d" % bid).get_data(as_text=True))
        self.t.post("/box/%d" % bid, data=dict(due="2099-01-01T10:00"))
        self.assertIn("המטלה הוגשה בזמן", self.t.get("/box/%d" % bid).get_data(as_text=True))

    def test_L23_review_alone_and_with_three_peers(self):
        bid = self.new_box()
        self.submit(self.s1, bid, "my own text about sorting algorithms")
        sid = one("SELECT id FROM submissions")[0]
        self.assertIn("אין עדיין עבודות", self.s1.post("/submission/%d/review" % sid).get_json()["review"])
        for i, c in enumerate(self.students[1:] + [self.register("student4", "תלמיד 4")]):
            c.post("/join", data={"code": self.code})
            self.submit(c, bid, "peer text %d about graphs and trees" % i)
        rev = self.s1.post("/submission/%d/review" % sid).get_json()["review"]
        self.assertIn("הושוו 3 עבודות אחרות", rev)
        for name in ("student2", "student3", "student4", "תלמיד 2", "תלמיד 3", "תלמיד 4"):
            self.assertNotIn(name, rev)


class Recordings(Fixture):
    def test_L24_summary_generated_once(self):
        self.t.post("/course/%d/summaries" % self.cid, data={
            "recording": (text_file("x", "lec.mp4")[0], "lec.mp4"),
            "transcript": text_file("Some lecture transcript sentence that is long enough. " * 5, "t.txt")},
            content_type="multipart/form-data")
        rid = one("SELECT id FROM recordings")[0]
        with mock.patch.object(ai, "summarize_lecture", wraps=ai.summarize_lecture) as spy:
            first = self.s1.post("/recording/%d/summary" % rid).get_json()["summary"]
            second = self.students[1].post("/recording/%d/summary" % rid).get_json()["summary"]
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(first, second)


class Join(Fixture):
    STUDENTS = 1

    def test_L25_join_twice_wrong_code_lowercase(self):
        self.s1.post("/join", data={"code": self.code})
        self.assertEqual(count("enroll"), 1)
        r = self.s1.post("/join", data={"code": "ZZZZZZ"})
        self.assertIn("קוד קורס לא נמצא", r.get_data(as_text=True))
        s2 = self.register("student2", "תלמיד 2")
        r = s2.post("/join", data={"code": self.code.lower()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(count("enroll"), 2)


class Persistence(Fixture):
    def test_L26_data_survives_restart(self):
        self.t.post(self.att("/start"))
        self.s1.post(self.att("/checkin"))
        self.t.post("/course/%d/notes" % self.cid, data=dict(topic="T", date="2000-01-01", start="09:00"))
        self.s1.post("/lesson/%d/comments" % one("SELECT id FROM lessons")[0], json={"body": "hi"})
        bid = self.new_box()
        self.submit(self.s1, bid, "work")
        code = ("import app\n"
                "with app.app.app_context():\n"
                "    print([app.q('SELECT COUNT(*) n FROM %s' % t, one=True)['n'] for t in "
                "('att_records','comments','submissions')])")
        env = dict(os.environ, SC_DATA=m.DATA)
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "[1, 1, 1]", out.stderr)
        sub = one("SELECT filename FROM submissions")[0]
        self.assertTrue(os.path.isfile(os.path.join(m.UPLOADS, sub)))


if __name__ == "__main__":
    unittest.main()
