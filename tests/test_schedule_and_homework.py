"""Course schedule rules (no overlapping courses for the same teacher) and homework-box rules
(unique name per course, teacher can delete a box)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Fixture, count, m, one  # noqa: E402

OVERLAP = "כבר קבוע ביום"


class CourseSchedule(Fixture):
    STUDENTS = 1  # fixture course: teacher, Sunday ("ראשון") 10:00-12:00 (default 2 hours)

    def create(self, name="קורס", day="ראשון", start="10:00", end="12:00", teacher=None):
        r = (teacher or self.t).post("/course/new", data=dict(name=name, day=day, time=start,
                                                               end_time=end))
        return r, r.get_data(as_text=True)

    def test_overlap_same_teacher_same_day_blocked(self):
        before = count("courses")
        for start, end in (("10:00", "12:00"), ("11:00", "13:00"), ("09:00", "10:30"),
                           ("09:00", "13:00"), ("10:30", "11:30")):
            r, html = self.create(start=start, end=end)
            self.assertEqual(r.status_code, 200, (start, end))
            self.assertIn(OVERLAP, html, (start, end))
        self.assertEqual(count("courses"), before)

    def test_back_to_back_allowed(self):
        self.assertEqual(self.create(start="12:00", end="14:00")[0].status_code, 302)
        self.assertEqual(self.create(start="14:00", end="16:00")[0].status_code, 302)
        self.assertEqual(self.create(start="08:00", end="10:00")[0].status_code, 302)

    def test_same_name_other_day_or_time_allowed(self):
        name = one("SELECT name FROM courses")[0]
        self.assertEqual(self.create(name=name, day="שני")[0].status_code, 302)
        self.assertEqual(self.create(name=name, start="16:00", end="18:00")[0].status_code, 302)
        self.assertIn(OVERLAP, self.create(name=name)[1])

    def test_other_teacher_same_slot_allowed(self):
        t2 = self.register("teacher2", "מורה 2", "teacher", m.TEACHER_CODE)
        self.assertEqual(self.create(teacher=t2)[0].status_code, 302)

    def test_end_must_be_after_start(self):
        for end in ("10:00", "09:00"):
            r, html = self.create(day="שני", start="10:00", end=end)
            self.assertIn("שעת הסיום חייבת להיות אחרי שעת ההתחלה", html)

    def test_form_keeps_values_after_error(self):
        _, html = self.create(name="קורס חדש", start="11:00", end="13:00")
        self.assertIn('value="קורס חדש"', html)
        self.assertIn("<option selected>ראשון</option>", html)
        self.assertIn('name="end_time" value="13:00"', html)

    def test_listing_shows_start_and_end(self):
        self.create(day="שני", start="14:00", end="15:30")
        self.assertIn('יום שני · <bdi dir="ltr">14:00–15:30</bdi>', self.t.get("/courses").get_data(as_text=True))

    def test_old_database_gets_end_time(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE courses(id INTEGER PRIMARY KEY, name TEXT, day TEXT, time TEXT, "
                  "code TEXT UNIQUE, teacher_id INT)")
        c.execute("INSERT INTO courses(name,day,time,code,teacher_id) VALUES('x','ראשון','10:00','A',1)")
        c.execute("INSERT INTO courses(name,day,time,code,teacher_id) VALUES('y','שני','23:00','B',1)")
        c.commit()
        c.close()
        with mock.patch.object(m, "DB_PATH", path):
            m.init_db()
            m.init_db()  # running twice is harmless
        c = sqlite3.connect(path)
        self.assertEqual(c.execute("SELECT end_time FROM courses ORDER BY id").fetchall(),
                         [("12:00",), ("23:59",)])
        c.close()


class HomeworkBoxes(Fixture):
    STUDENTS = 2

    def test_duplicate_name_blocked_in_same_course(self):
        self.new_box("מטלה 1")
        for dup in ("מטלה 1", "  מטלה 1  "):
            r = self.t.post("/course/%d/homework" % self.cid,
                            data=dict(name=dup, due="2099-02-01T10:00"), follow_redirects=True)
            self.assertIn("כבר קיימת מטלה בשם", r.get_data(as_text=True))
        self.t.post("/course/%d/homework" % self.cid, data=dict(name="Task A", due="2099-02-01T10:00"))
        r = self.t.post("/course/%d/homework" % self.cid, data=dict(name="task a", due="2099-02-01T10:00"),
                        follow_redirects=True)
        self.assertIn("כבר קיימת מטלה בשם", r.get_data(as_text=True))
        self.assertEqual(count("boxes"), 2)

    def test_same_name_allowed_in_other_course(self):
        self.new_box("מטלה 1")
        other = self.new_course(self.t, "קורס 2", day="שני")
        self.t.post("/course/%d/homework" % other, data=dict(name="מטלה 1", due="2099-02-01T10:00"))
        self.assertEqual(count("boxes"), 2)

    def test_empty_name_rejected(self):
        r = self.t.post("/course/%d/homework" % self.cid, data=dict(name="   ", due="2099-02-01T10:00"),
                        follow_redirects=True)
        self.assertIn("יש להזין שם למטלה", r.get_data(as_text=True))
        self.assertEqual(count("boxes"), 0)

    def test_teacher_deletes_box_with_submissions(self):
        bid = self.new_box()
        keep = self.new_box("מטלה 2")
        for c in self.students:
            self.submit(c, bid, "my work")
        self.submit(self.s1, keep, "other work")
        files = [one("SELECT filename FROM submissions WHERE id=?", sid)[0]
                 for (sid,) in sqlite3.connect(m.DB_PATH).execute(
                     "SELECT id FROM submissions WHERE box_id=?", (bid,))]
        self.assertIn('action="/box/%d/delete"' % bid, self.t.get("/box/%d" % bid).get_data(as_text=True))
        r = self.t.post("/box/%d/delete" % bid, follow_redirects=True)
        self.assertIn("נמחקה", r.get_data(as_text=True))
        self.assertIsNone(one("SELECT 1 FROM boxes WHERE id=?", bid))
        self.assertEqual(count("submissions", "box_id=?", bid), 0)
        self.assertEqual(count("submissions", "box_id=?", keep), 1)
        for f in files:
            self.assertFalse(os.path.exists(os.path.join(m.UPLOADS, f)))
        self.assertEqual(self.t.get("/box/%d" % bid).status_code, 404)

    def test_student_and_other_teacher_cannot_delete(self):
        bid = self.new_box()
        self.assertNotIn("/box/%d/delete" % bid, self.s1.get("/box/%d" % bid).get_data(as_text=True))
        self.assertEqual(self.s1.post("/box/%d/delete" % bid).status_code, 403)
        t2 = self.register("teacher2", "מורה 2", "teacher", m.TEACHER_CODE)
        self.assertEqual(t2.post("/box/%d/delete" % bid).status_code, 403)
        self.assertEqual(count("boxes"), 1)


if __name__ == "__main__":
    unittest.main()
