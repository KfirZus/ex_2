"""STD section 7 - creates the load-test students load1..load40 (password 1234) and enrolls
them in course 1 (the DEMO01 course from seed_demo.py).

Run on the server machine, after seed_demo.py:
    python loadtest/seed_load.py            # 40 students
    python loadtest/seed_load.py 100        # any other number (e.g. STP L-04 spike)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.security import generate_password_hash  # noqa: E402

from app import app, db, init_db, q, run  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
COURSE_ID = 1

init_db()
with app.app_context():
    if not q("SELECT 1 FROM courses WHERE id=?", COURSE_ID, one=True):
        sys.exit("Course %d not found - run `python seed_demo.py` first." % COURSE_ID)
    pw = generate_password_hash("1234")  # same hash for all: seeding stays fast
    created = 0
    for i in range(1, N + 1):
        name = "load%d" % i
        row = q("SELECT id FROM users WHERE username=?", name, one=True)
        uid = row["id"] if row else run("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)",
                                        name, pw, "student", "עומס %d" % i)
        created += 0 if row else 1
        db().execute("INSERT OR IGNORE INTO enroll VALUES(?,?)", (COURSE_ID, uid))
    db().commit()
    print("Load users ready: %d total, %d new, enrolled in course %d." % (N, created, COURSE_ID))
