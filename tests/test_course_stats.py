"""Course dashboard statistics: attendance and homework numbers for teachers and students."""
import os
import sys
import time
import unittest

from flask import g

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Fixture, m, sql  # noqa: E402


class CourseStats(Fixture):
    STUDENTS = 3

    def stats(self, username):
        with m.app.test_request_context():
            g.user = m.q("SELECT * FROM users WHERE username=?", username, one=True)
            return m.course_stats(self.cid)

    def session(self, present, ago=3600, running=False):
        """Adds an attendance check that started `ago` seconds ago with the given students."""
        now = time.time()
        ends = now + 300 if running else now - ago + 600
        sql("INSERT INTO att_sessions(course_id,started,ends) VALUES(?,?,?)", self.cid, now - ago, ends)
        sid = m.sqlite3.connect(m.DB_PATH).execute("SELECT MAX(id) FROM att_sessions").fetchone()[0]
        for name in present:
            sql("INSERT INTO att_records VALUES(?,?,?)", sid, self.uid(name), now - ago + 10)
        return sid

    def test_empty_course(self):
        st = self.stats("teacher")
        self.assertEqual((st["att"]["sessions"], st["att"]["avg"], st["hw"]["published"]), (0, 0, 0))
        html = self.t.get("/course/%d" % self.cid).get_data(as_text=True)
        self.assertIn("עדיין לא בוצעו בדיקות נוכחות בקורס.", html)
        self.assertIn("עדיין לא פורסמו מטלות בקורס.", html)

    def test_teacher_attendance_average_and_bars(self):
        self.session(["student1", "student2", "student3"], ago=7200)  # 3/3
        self.session(["student1"], ago=3600)                          # 1/3
        st = self.stats("teacher")["att"]
        self.assertEqual(st["sessions"], 2)
        self.assertEqual(st["avg"], 67)  # 4 of 6 possible check-ins
        self.assertEqual([b["value"] for b in st["bars"]], [100, 33])  # oldest first
        self.assertEqual(st["last"]["note"], "1/3")

    def test_running_check_not_counted_until_it_ends(self):
        self.session(["student1"], running=True)
        self.assertEqual(self.stats("teacher")["att"]["sessions"], 0)
        self.assertEqual(self.stats("student2")["att"]["sessions"], 0)   # not missed yet
        self.assertEqual(self.stats("student1")["att"]["attended"], 1)   # already checked in

    def test_student_own_attendance(self):
        self.session(["student1"], ago=7200)
        self.session(["student2"], ago=3600)
        st = self.stats("student1")["att"]
        self.assertEqual((st["attended"], st["sessions"], st["missed"], st["avg"]), (1, 2, 1, 50))
        self.assertEqual([b["present"] for b in st["bars"]], [True, False])

    def test_teacher_homework_counts(self):
        b1 = self.new_box("מטלה 1", past=True)
        b2 = self.new_box("מטלה 2")
        self.submit(self.s1, b2, "on time")
        self.submit(self.students[1], b2, "on time")
        sql("INSERT INTO submissions(box_id,user_id,filename,orig,ts,text) VALUES(?,?,?,?,?,?)",
            b1, self.uid("student3"), "x", "x.txt", time.time(), "late")  # after the past due
        hw = self.stats("teacher")["hw"]
        self.assertEqual((hw["published"], hw["open"], hw["subs"], hw["rate"]), (2, 1, 3, 50))
        rows = {r["b"]["id"]: r for r in hw["rows"]}
        self.assertEqual((rows[b2]["note"], rows[b2]["open"]), ("2/3", True))
        self.assertEqual((rows[b1]["late"], rows[b1]["open"]), (1, False))

    def test_student_homework_status(self):
        done, late, open_, missed = (self.new_box("א"), self.new_box("ב", past=True),
                                     self.new_box("ג"), self.new_box("ד", past=True))
        self.submit(self.s1, done, "x")
        sql("INSERT INTO submissions(box_id,user_id,filename,orig,ts,text) VALUES(?,?,?,?,?,?)",
            late, self.uid("student1"), "x", "x.txt", time.time(), "x")
        hw = self.stats("student1")["hw"]
        status = {r["b"]["id"]: r["status"] for r in hw["rows"]}
        self.assertEqual(status, {done: "done", late: "late", open_: "open", missed: "missed"})
        self.assertEqual((hw["published"], hw["submitted"], hw["late"], hw["open"], hw["missed"]),
                         (4, 2, 1, 1, 1))

    def test_student_sees_only_own_numbers(self):
        bid = self.new_box()
        self.submit(self.students[1], bid, "x")
        html = self.s1.get("/course/%d" % self.cid).get_data(as_text=True)
        self.assertIn("ממתין להגשה", html)
        self.assertNotIn("1/3 הגישו", html)
        self.assertIn("1/3 הגישו", self.t.get("/course/%d" % self.cid).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
