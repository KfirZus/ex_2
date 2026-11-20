"""STD section 7 - Locust scenarios for L-02 (live poll) and L-03 (attendance burst).

    pip install locust
    # L-02: 40 students in a live poll (teacher starts the poll first; set RUN to its run id)
    locust -f loadtest/locustfile.py Student --host http://IP:5000 --users 40 --spawn-rate 10 \
           --run-time 10m --headless --csv results/l02
    # L-03: 40 students press "I'm present" within ~5 seconds (teacher starts attendance first)
    locust -f loadtest/locustfile.py CheckinBurst --host http://IP:5000 --users 40 --spawn-rate 40 \
           --run-time 20s --headless --csv results/l03

Users are load1..loadN (password 1234) from loadtest/seed_load.py. Request names match the rows
the STD reads from the CSV: /poll-run/state, /poll-run/answer, /attendance/checkin.
"""
import itertools
import os
import random
import threading

from locust import HttpUser, constant, task
from locust.exception import StopUser

RUN = int(os.environ.get("RUN", 1))          # poll run id from /poll-run/<id>
COURSE = int(os.environ.get("COURSE", 1))
PASSWORD = "1234"
CHANGE_ANSWER_RATE = 0.3                     # STP L-02: some students change their answer

_ids = itertools.count(1)
_lock = threading.Lock()


def next_username():
    with _lock:
        return "load%d" % next(_ids)


class _LoggedIn(HttpUser):
    abstract = True

    def on_start(self):
        self.username = next_username()
        with self.client.post("/login", data={"username": self.username, "password": PASSWORD},
                              name="/login", allow_redirects=False, catch_response=True) as r:
            if r.status_code != 302:
                r.failure("login failed for %s (%s)" % (self.username, r.status_code))
                raise StopUser()


class Student(_LoggedIn):
    """Polls the live state every 0.7 s and answers once per question (sometimes changes it)."""
    wait_time = constant(0.7)

    def on_start(self):
        super().on_start()
        self.answered = {}

    @task
    def poll(self):
        with self.client.get("/poll-run/%d/state" % RUN, name="/poll-run/state",
                             catch_response=True) as r:
            if r.status_code != 200:
                r.failure("state %s" % r.status_code)
                return
            st = r.json()
        if st.get("waiting") or st.get("finished") or st.get("phase") != "answer":
            return
        idx, n = st["idx"], len(st["options"])
        times = self.answered.get(idx, 0)
        if times == 0 or (times == 1 and random.random() < CHANGE_ANSWER_RATE):
            self.answer(random.randrange(n))
            self.answered[idx] = times + 1

    def answer(self, opt):
        with self.client.post("/poll-run/%d/answer" % RUN, json={"opt": opt}, name="/poll-run/answer",
                              catch_response=True) as r:
            # 400 = the answer window closed between /state and /answer: expected, not a server error
            if r.status_code == 400:
                r.success()


class CheckinBurst(_LoggedIn):
    """Each user checks in once, right after logging in, then stays idle."""
    wait_time = constant(60)

    def on_start(self):
        super().on_start()
        self.done = False

    @task
    def checkin(self):
        if self.done:
            return
        with self.client.post("/course/%d/attendance/checkin" % COURSE, name="/attendance/checkin",
                              catch_response=True) as r:
            if r.status_code != 200 or not r.json().get("ok"):
                r.failure("checkin not recorded (%s %s)" % (r.status_code, r.text[:80]))
        self.done = True
