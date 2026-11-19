"""Creates demo users and a demo course so the class demo works immediately.
Run once:  python seed_demo.py
Teacher: teacher / 1234      Students: student1..student5 / 1234     Course join code: DEMO01
"""
from werkzeug.security import generate_password_hash

from app import app, db, init_db, q, run

init_db()
with app.app_context():
    if not q("SELECT 1 FROM users WHERE username='teacher'", one=True):
        tid = run("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)",
                  "teacher", generate_password_hash("1234"), "teacher", "המרצה")
        cid = run("INSERT INTO courses(name,day,time,code,teacher_id) VALUES(?,?,?,?,?)",
                  "מבוא להנדסת תוכנה", "ראשון", "10:00", "DEMO01", tid)
        for i in range(1, 6):
            uid = run("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)",
                      "student%d" % i, generate_password_hash("1234"), "student", "תלמיד %d" % i)
            db().execute("INSERT OR IGNORE INTO enroll VALUES(?,?)", (cid, uid))
        db().commit()
        print("Demo data created.")
    else:
        print("Demo data already exists.")
