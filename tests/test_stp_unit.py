"""STP section 4 - unit tests U-01 .. U-20.

Cases marked "expected to fail today" in the STP use @unittest.expectedFailure: they describe the
correct behaviour, so they turn into "unexpected success" once the code is fixed - then remove
the decorator.
"""
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Fixture, ai, count, m, one, sql  # noqa: E402
from werkzeug.datastructures import FileStorage  # noqa: E402
import io  # noqa: E402

DUE = 1_800_000_000.0


def poll(n):
    qs = [dict(text="Q%d" % i, options=["a", "b"], correct=1) for i in range(n)]
    return {"data": json.dumps(qs)}


def state_at(elapsed, n=2):
    now = 1_000_000.0
    with mock.patch.object(m.time, "time", return_value=now):
        return m.poll_run_state({"started": now - elapsed}, poll(n))


class U01_U02_LateLabel(unittest.TestCase):
    def test_U01_exactly_on_due_is_on_time(self):
        self.assertEqual(m.late_label({"due": DUE}, DUE), ("המטלה הוגשה בזמן", False))

    def test_U02_hours_round_up(self):
        for delta, hours in ((1, 1), (59 * 60, 1), (61 * 60, 2), (2 * 3600 + 60, 3)):
            label, late = m.late_label({"due": DUE}, DUE + delta)
            self.assertTrue(late, delta)
            self.assertEqual(label, "המטלה הוגשה באיחור (%d שעות)" % hours, delta)


class U03_U04_PollState(unittest.TestCase):
    def test_U03_phase_boundaries(self):
        self.assertEqual((state_at(9.9)["idx"], state_at(9.9)["phase"]), (0, "answer"))
        self.assertEqual((state_at(10.0)["idx"], state_at(10.0)["phase"]), (0, "reveal"))
        self.assertEqual((state_at(14.9)["idx"], state_at(14.9)["phase"]), (0, "reveal"))
        self.assertEqual((state_at(15.0)["idx"], state_at(15.0)["phase"]), (1, "answer"))

    def test_U04_finished_after_n_times_15(self):
        self.assertTrue(state_at(3 * 15, n=3)["finished"])
        self.assertFalse(state_at(3 * 15 - 0.1, n=3)["finished"])


class U05_U09_DbHelpers(Fixture):
    STUDENTS = 0

    def test_U05_run_counts_ignores_out_of_range(self):
        for uid, opt in ((1, -1), (2, 99), (3, 1), (4, 1)):
            sql("INSERT INTO answers VALUES(7,0,?,?)", uid, opt)
        with m.app.app_context():
            self.assertEqual(m.run_counts(7, 0, 2), [0, 2])

    def test_U09_active_session_returns_only_live_one(self):
        now = time.time()
        sql("INSERT INTO att_sessions(course_id,started,ends) VALUES(?,?,?)", self.cid, now - 900, now - 300)
        sql("INSERT INTO att_sessions(course_id,started,ends) VALUES(?,?,?)", self.cid, now, now + 600)
        live = one("SELECT MAX(id) FROM att_sessions")[0]
        with m.app.app_context():
            self.assertEqual(m.active_session(self.cid)["id"], live)
            sql("UPDATE att_sessions SET ends=?", now - 1)
            self.assertIsNone(m.active_session(self.cid))


class U06_LessonOpen(unittest.TestCase):
    def lesson(self, dt):
        return {"date": dt.strftime("%Y-%m-%d"), "start": dt.strftime("%H:%M")}

    def test_U06_future_now_past(self):
        now = datetime.now()
        self.assertFalse(m.lesson_open(self.lesson(now + timedelta(hours=1))))
        self.assertTrue(m.lesson_open(self.lesson(now)))
        self.assertTrue(m.lesson_open(self.lesson(now - timedelta(days=1))))


class U07_U08_Network(unittest.TestCase):
    def on_net(self, addr, headers=None):
        with m.app.test_request_context("/", environ_base={"REMOTE_ADDR": addr}, headers=headers or {}):
            return m.on_school_network()

    def test_U07_default_ranges(self):
        for addr, ok in (("127.0.0.1", True), ("192.168.1.5", True), ("10.9.14.244", True),
                         ("8.8.8.8", False), ("not-an-ip", False)):
            self.assertEqual(self.on_net(addr), ok, addr)

    @unittest.expectedFailure  # STP: expected to fail today (defect D-01)
    def test_U08_forwarded_header_does_not_bypass(self):
        self.assertFalse(self.on_net("8.8.8.8", {"X-Forwarded-For": "192.168.1.5"}))


class U10_U11_Files(unittest.TestCase):
    def save(self, filename):
        fs = FileStorage(stream=io.BytesIO(b"data"), filename=filename)
        return m.save_upload(fs)

    def test_U10_save_upload_names_are_clean_unique_and_contained(self):
        names = [self.save(n) for n in ("עבודה.txt", "../../evil.txt", "..\\..\\x.txt", "")]
        self.assertEqual(len(set(names)), 4)
        uploads = os.path.realpath(m.UPLOADS)
        for n in names:
            self.assertNotIn("/", n)
            self.assertNotIn("\\", n)
            self.assertNotIn("..", n)
            self.assertEqual(os.path.dirname(os.path.realpath(os.path.join(m.UPLOADS, n))), uploads)
            self.assertTrue(os.path.isfile(os.path.join(m.UPLOADS, n)))
        self.assertTrue(names[3].endswith("_file"))

    def test_U11_read_text_never_raises(self):
        p = os.path.join(m.UPLOADS, "u11.bin")
        with open(p, "wb") as f:
            f.write(bytes(range(256)))
        self.assertIsInstance(m.read_text(p), str)
        with open(p, "wb") as f:
            f.write("שלום".encode("cp1255"))
        self.assertIsInstance(m.read_text(p), str)
        self.assertEqual(m.read_text(os.path.join(m.UPLOADS, "missing.txt")), "")


class U12_U16_Ai(unittest.TestCase):
    SENT = "Sentence number %d explains an important software engineering idea clearly."

    def test_U12_extractive(self):
        short = ai._extractive(self.SENT % 1)
        self.assertTrue(short.startswith("• "))
        long_text = " ".join(self.SENT % i for i in range(30))
        bullets = [l for l in ai._extractive(long_text).splitlines() if l.startswith("• ")]
        self.assertLessEqual(len(bullets), 7)
        vtt = "".join("%d\n00:00:%02d.000 --> 00:00:%02d.500\n%s\n\n" % (i, i, i, self.SENT % i)
                      for i in range(1, 12))
        out = ai._extractive(vtt)
        self.assertNotIn("-->", out)
        self.assertNotRegex(out, r"\d\d:\d\d:\d\d")

    def test_U13_empty_transcript_message(self):
        self.assertIn("תמלול", ai.summarize_lecture("   "))

    def test_U14_similarity(self):
        t = "binary search trees insertion deletion balancing"
        self.assertEqual(ai.similarity(t, t), 100)
        self.assertLess(ai.similarity(t, "qqqq wwww zzzz xxxx yyyy"), 30)
        self.assertEqual(ai.similarity("", "some text"), 0)

    @unittest.expectedFailure  # new defect found by U-14: two empty texts score 50 (difflib ratio 1.0)
    def test_U14_similarity_both_empty(self):
        self.assertEqual(ai.similarity("", ""), 0)

    def test_U15_review_homework(self):
        self.assertIn("אין עדיין עבודות", ai.review_homework("", "my work text here", []))
        t = "binary search trees insertion deletion " * 10
        self.assertIn("מומלץ לבדוק חפיפה", ai.review_homework("", t, [("עבודה 1", t)]))

    def test_U16_count_people_never_raises(self):
        self.assertIsNone(ai.count_people("whatever.jpg"))  # no API key
        bad = os.path.join(m.UPLOADS, "broken.jpg")
        with open(bad, "wb") as f:
            f.write(b"not an image")
        with mock.patch.object(ai, "API_KEY", "test-key"), \
                mock.patch.object(ai, "_call", side_effect=OSError("network down")):
            self.assertIsNone(ai.count_people(bad))
            self.assertIsNone(ai.count_people(os.path.join(m.UPLOADS, "missing.jpg")))


class U17_PollAnswerInput(Fixture):
    STUDENTS = 1

    def setUp(self):
        super().setUp()
        self.save_poll([{"text": "Q", "options": [{"text": "a"}, {"text": "b", "correct": True}]}])
        self.rid = self.start_poll(one("SELECT id FROM polls")[0])
        self.at(self.rid, 2)

    def answer(self, body):
        return self.s1.post("/poll-run/%d/answer" % self.rid, json=body)

    @unittest.expectedFailure  # STP: expected to fail today (int("abc") -> 500)
    def test_U17_non_numeric_opt(self):
        self.assertEqual(self.answer({"opt": "abc"}).status_code, 400)
        self.assertEqual(count("answers"), 0)

    @unittest.expectedFailure  # STP: expected to fail today (out-of-range opt is stored)
    def test_U17_out_of_range_opt(self):
        self.assertEqual(self.answer({"opt": 99}).status_code, 400)
        self.assertEqual(count("answers"), 0)

    @unittest.expectedFailure  # STP: expected to fail today (missing opt stored as -1)
    def test_U17_missing_opt(self):
        self.assertEqual(self.answer({}).status_code, 400)
        self.assertEqual(count("answers"), 0)


class U18_DueDateInput(Fixture):
    STUDENTS = 0

    def test_U18_bad_due_on_create(self):
        r = self.t.post("/course/%d/homework" % self.cid, data=dict(name="x", due="tomorrow"),
                        follow_redirects=True)
        self.assertLess(r.status_code, 500)
        self.assertIn("תאריך או שעה לא תקינים", r.get_data(as_text=True))
        self.assertEqual(count("boxes"), 0)

    def test_U18_empty_due_on_edit(self):
        bid = self.new_box()
        due = one("SELECT due FROM boxes")[0]
        r = self.t.post("/box/%d" % bid, data=dict(due=""), follow_redirects=True)
        self.assertLess(r.status_code, 500)
        self.assertIn("תאריך או שעה לא תקינים", r.get_data(as_text=True))
        self.assertEqual(one("SELECT due FROM boxes")[0], due)


class U19_Fmt(unittest.TestCase):
    def test_U19_fmt(self):
        self.assertEqual(m.fmt(0), "")
        self.assertEqual(m.fmt(None), "")
        ts = datetime(2026, 3, 5, 9, 7).timestamp()
        self.assertEqual(m.fmt(ts), "05/03/2026 09:07")


class U20_PollSaveInput(Fixture):
    STUDENTS = 0

    def test_U20_validation_and_truncation(self):
        two = [{"text": "a"}, {"text": "b"}]
        self.assertEqual(self.save_poll([]).status_code, 400)
        self.assertEqual(self.save_poll([{"text": " ", "options": two}]).status_code, 400)
        self.assertEqual(self.save_poll([{"text": "Q", "options": [{"text": "a"}]}]).status_code, 400)
        self.assertEqual(count("polls"), 0)
        five = [{"text": str(i)} for i in range(5)]
        self.assertTrue(self.save_poll([{"text": "Q", "options": five}]).get_json()["ok"])
        data = json.loads(one("SELECT data FROM polls")[0])
        self.assertEqual(data[0]["options"], ["0", "1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
