"""STD sections 4-6 - automated server-side version of E-01, E-02, N-04, N-05 and N-06.

What these tests cannot check (the browser UI, timer animation, QR camera, Windows firewall,
real devices on the Wi-Fi: N-01..N-03, N-07, and the confirm() labels in E-02 step 20) stays
manual and is run from the STD. Permission cases P-xx live in test_stp_permissions.py and the
load cases L-02/L-03 in loadtest/.
"""
import csv
import io
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, Fixture, count, jpeg, m, one, page_json, sql  # noqa: E402


class E01_FullLesson(Fixture):
    STUDENTS = 0

    def test_E01_full_lesson(self):
        t = self.t
        # 1. create course
        r = t.post("/course/new", data=dict(name="בדיקת E-01", day="ראשון", time="10:00"), follow_redirects=True)
        cid = one("SELECT MAX(id) FROM courses")[0]
        html = r.get_data(as_text=True)
        self.assertIn("בדיקת E-01", html)
        self.assertIn("ראשון · 10:00", html)
        # 2. add people: 6-char code and join URL
        code = one("SELECT code FROM courses WHERE id=?", cid)[0]
        self.assertRegex(code, r"^[A-Z0-9]{6}$")
        page = t.get("/course/%d/add-people" % cid).get_data(as_text=True)
        self.assertIn(code, page)
        self.assertIn("/join?code=" + code, page)
        # 3. four students join; teacher sees 4 participants
        st = [self.register("student%d" % i, "תלמיד %d" % i) for i in range(1, 5)]
        for c in st:
            self.assertTrue(c.post("/join", data={"code": code}).headers["Location"].endswith("/course/%d" % cid))
        self.assertIn("משתתפים (4)", t.get("/course/%d/more" % cid).get_data(as_text=True))
        # 4. start attendance: 10 minutes, counter 0
        t.post(self.att("/start", cid))
        started, ends = one("SELECT started, ends FROM att_sessions")
        self.assertAlmostEqual(ends - started, 600, delta=1)
        status = t.get(self.att("/status", cid)).get_json()
        self.assertEqual((status["active"], status["count"]), (True, 0))
        self.assertGreaterEqual(status["remaining"], 598)
        # 5. three students check in, student3 twice
        for c in st[:3]:
            self.assertTrue(c.post(self.att("/checkin", cid)).get_json()["ok"])
        st[2].post(self.att("/checkin", cid))
        # 6. live counter
        self.assertEqual(t.get(self.att("/status", cid)).get_json()["count"], 3)
        # 7. photo
        t.post(self.att("/photo", cid), data={"photo": jpeg()}, content_type="multipart/form-data")
        html = t.get(self.att("", cid)).get_data(as_text=True)
        self.assertIn("תמונות נוכחות (1)", html)
        self.assertIn("לא זמין (נדרש מפתח AI)", html)
        self.assertIn("כמות תלמידים שנרשמו בנוכחות:</b> 3", html)
        # 8. timer ends -> start button active again
        self.end_attendance()
        self.assertFalse(t.get(self.att("/status", cid)).get_json()["active"])
        # 9. late check-in does nothing
        self.assertFalse(st[3].post(self.att("/checkin", cid)).get_json()["ok"])
        self.assertEqual(count("att_records"), 3)
        # 10. student history + class photo visible to student
        self.assertIn("היית נוכח בתאריך", st[0].get(self.att("", cid)).get_data(as_text=True))
        s4 = st[3].get(self.att("", cid)).get_data(as_text=True)
        self.assertIn("לא היית נוכח בתאריך", s4)
        photo = one("SELECT filename FROM att_photos")[0]
        self.assertEqual(st[0].get("/media/" + photo).status_code, 200)
        # 11. CSV export
        r = t.get(self.att("/export.csv", cid))
        self.assertIn("attendance_course_%d.csv" % cid, r.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
        self.assertEqual(rows[0][:2] + rows[0][-2:], ["שם", "שם משתמש", 'סה"כ נוכחות', "אחוז"])
        self.assertEqual(len(rows) - 1, 4)
        by_user = {row[1]: row[2:] for row in rows[1:]}
        for u in ("student1", "student2", "student3"):
            self.assertEqual(by_user[u], ["V", "1", "100%"])
        self.assertEqual(by_user["student4"], ["X", "0", "0%"])
        # 12. student cannot export
        self.assertEqual(st[0].get(self.att("/export.csv", cid)).status_code, 403)


class E02_LivePoll(Fixture):
    QUESTIONS = [
        {"text": "מהי בירת ישראל?", "options": [{"text": "תל אביב"}, {"text": "ירושלים", "correct": True},
                                                 {"text": "חיפה"}, {"text": "באר שבע"}]},
        {"text": "כמה זה 2+2?", "options": [{"text": "3"}, {"text": "4", "correct": True}]},
    ]

    def test_E02_live_poll(self):
        s1, s2, s3 = self.students
        polls_url = "/course/%d/polls" % self.cid
        # 5. invalid third question rejected, nothing saved
        bad = self.QUESTIONS + [{"text": "שאלה 3", "options": [{"text": "רק אחת"}]}]
        r = self.save_poll(bad, title="סקר E-02")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "לכל שאלה נדרשים טקסט ולפחות שתי תשובות")
        self.assertEqual(count("polls"), 0)
        # 6. save
        self.assertTrue(self.save_poll(self.QUESTIONS, title="סקר E-02").get_json()["ok"])
        pid = one("SELECT id FROM polls")[0]
        page = self.t.get(polls_url).get_data(as_text=True)
        self.assertIn("סקר E-02", page)
        self.assertIn("2 שאלות", page)
        # 7. students have no teacher controls
        sp = s1.get(polls_url).get_data(as_text=True)
        self.assertNotIn("/polls/%d/start" % pid, sp)
        self.assertNotIn("/polls/%d/delete" % pid, sp)
        self.assertNotIn("/polls/new", sp)
        self.assertEqual(s1.post("/course/%d/polls/%d/start" % (self.cid, pid)).status_code, 403)
        # 8. start: short waiting phase, teacher cannot answer
        rid = self.start_poll(pid)
        self.assertTrue(self.t.get("/poll-run/%d/state" % rid).get_json()["waiting"])
        st = self.at(rid, 0.5, self.t)
        self.assertEqual((st["idx"], st["phase"], st["total"]), (0, "answer", 2))
        self.assertEqual(self.t.post("/poll-run/%d/answer" % rid, json={"opt": 1}).status_code, 400)
        # 9. banner for students
        self.assertIn("יש סקר פעיל - הצטרפו עכשיו", s1.get(polls_url).get_data(as_text=True))
        # 10. Q1 answers, s1 changes its answer
        ans = "/poll-run/%d/answer" % rid
        self.at(rid, 1)
        s1.post(ans, json={"opt": 0})
        self.at(rid, 4)
        s1.post(ans, json={"opt": 1})
        s2.post(ans, json={"opt": 1})
        s3.post(ans, json={"opt": 2})
        self.assertEqual(self.at(rid, 5)["mine"], 1)
        self.assertNotIn("correct", self.at(rid, 9.5))
        # 11. reveal at 10s for 5s
        st = self.at(rid, 10.2, s2)
        self.assertEqual((st["phase"], st["correct"], st["counts"]), ("reveal", 1, [0, 2, 1, 0]))
        self.assertLessEqual(st["remaining"], 5)
        # 12. answers locked during reveal
        self.assertEqual(s2.post(ans, json={"opt": 3}).status_code, 400)
        self.assertEqual(self.at(rid, 14, s2)["counts"], [0, 2, 1, 0])
        # 13-14. Q2 starts automatically, s2 does not answer
        st = self.at(rid, 15.2)
        self.assertEqual((st["idx"], st["phase"]), (1, "answer"))
        s1.post(ans, json={"opt": 1})
        s3.post(ans, json={"opt": 0})
        st = self.at(rid, 25.5, s2)
        self.assertEqual((st["correct"], st["counts"], st["mine"]), (1, [1, 1], None))
        # 15. finished after 30s
        self.assertFalse(self.at(rid, 29.5).get("finished"))
        self.assertTrue(self.at(rid, 30)["finished"])
        # 16. exact rows in the DB
        c = __import__("sqlite3").connect(m.DB_PATH)
        rows = c.execute("SELECT qidx,opt,COUNT(*) FROM answers GROUP BY qidx,opt").fetchall()
        c.close()
        self.assertEqual(rows, [(0, 1, 2), (0, 2, 1), (1, 0, 1), (1, 1, 1)])
        # 17-19. review (and reopening gives the same view)
        for _ in range(2):
            d1 = page_json(s1.get("/poll/%d/review" % pid).get_data(as_text=True))
            self.assertEqual([(q["correct"], q["mine"], q["counts"]) for q in d1],
                             [(1, 1, [0, 2, 1, 0]), (1, 1, [1, 1])])
        d2 = page_json(s2.get("/poll/%d/review" % pid).get_data(as_text=True))
        self.assertEqual([q["mine"] for q in d2], [1, None])
        # 20. delete (server side; the yes/no dialog labels are a manual check, defect D-02)
        self.t.post("/course/%d/polls/%d/delete" % (self.cid, pid))
        self.assertNotIn("סקר E-02", s1.get(polls_url).get_data(as_text=True))


class N04_WifiGate(Fixture):
    STUDENTS = 1

    def test_N04_outside_network_does_nothing(self):
        m.ALLOWED_NETS[:] = [m.ipaddress.ip_network("192.168.1.0/24")]
        self.t.post(self.att("/start"))  # starting is not network-restricted
        self.assertTrue(self.t.get(self.att("/status")).get_json()["active"])
        page = self.s1.get(self.att(), environ_base={"REMOTE_ADDR": "172.20.10.3"})
        self.assertEqual(page.status_code, 200)
        r = self.s1.post(self.att("/checkin"), environ_base={"REMOTE_ADDR": "172.20.10.3"})
        self.assertEqual((r.status_code, r.get_json()), (200, {"ok": False}))
        self.assertEqual(count("att_records"), 0)
        # step 5-6: allow the hotspot range, new session, check-in succeeds
        m.ALLOWED_NETS[:] = [m.ipaddress.ip_network("172.20.10.0/28")]
        self.end_attendance()
        self.t.post(self.att("/start"))
        r = self.s1.post(self.att("/checkin"), environ_base={"REMOTE_ADDR": "172.20.10.3"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(count("att_records"), 1)


class N05_AllowedNetworksSetting(unittest.TestCase):
    CHECK = ("import app,ipaddress as i,sys;"
             "print([any(i.ip_address(a) in n for n in app.ALLOWED_NETS) for a in sys.argv[1:]])")

    def run_app(self, nets, *addrs):
        env = {k: v for k, v in os.environ.items() if k != "ALLOWED_NETWORKS"}
        env["SC_DATA"] = m.DATA
        if nets is not None:
            env["ALLOWED_NETWORKS"] = nets
        return subprocess.run([sys.executable, "-c", self.CHECK, *addrs], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_N05_single_range(self):
        out = self.run_app("192.168.1.0/24", "192.168.1.77", "192.168.2.5", "10.9.14.244", "172.20.10.13")
        self.assertEqual(out.stdout.strip(), "[True, False, False, False]", out.stderr)

    def test_N05_default_ranges(self):
        out = self.run_app(None, "192.168.1.77", "10.9.14.244", "172.20.10.13", "8.8.8.8", "127.0.0.1")
        self.assertEqual(out.stdout.strip(), "[True, True, True, False, True]", out.stderr)

    def test_N05_two_ranges_with_space(self):
        out = self.run_app("192.168.1.0/24, 172.20.10.0/28",
                           "192.168.1.77", "192.168.2.5", "10.9.14.244", "172.20.10.13")
        self.assertEqual(out.stdout.strip(), "[True, False, False, True]", out.stderr)

    def test_N05_invalid_value_fails_fast(self):
        out = self.run_app("abc", "127.0.0.1")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ValueError", out.stderr)
        self.assertIn("'abc'", out.stderr)


class N06_SpoofedHeader(Fixture):
    STUDENTS = 1

    @unittest.expectedFailure  # STD: expected to fail today (defect D-01)
    def test_N06_spoofed_forwarded_for_from_real_device(self):
        m.ALLOWED_NETS[:] = [m.ipaddress.ip_network("192.168.1.0/24")]
        self.t.post(self.att("/start"))
        device = {"REMOTE_ADDR": "172.20.10.3"}
        self.assertFalse(self.s1.post(self.att("/checkin"), environ_base=device).get_json()["ok"])
        r = self.s1.post(self.att("/checkin"), environ_base=device,
                         headers={"X-Forwarded-For": "192.168.1.50"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(count("att_records"), 0)


if __name__ == "__main__":
    unittest.main()
