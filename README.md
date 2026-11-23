# כיתה חכמה – Smart Classroom (Flask, Hebrew RTL)

Implements the SRS "Smart Classroom Management Web Application" and Ex2 (I2SE).

## Run
    pip install flask
    python seed_demo.py        # demo teacher/students/course (optional)
    python app.py              # http://<your-ip>:5000   (PORT=5055 to change)

Demo logins (after seed): `teacher / 1234`, `student1..5 / 1234`, course code `DEMO01`.
New teachers register with the teacher code (env `TEACHER_CODE`, default `teacher123`).

Phones: open `http://<laptop-ip>:5000` on the same Wi-Fi, then "Add to Home screen" (installable PWA).
Live camera QR scanning needs HTTPS on phones: run `HTTPS=1 python app.py` (self-signed, needs `cryptography`).
Without it the join screen still works via "צילום קוד QR" (photo of the code) or typing the code.

## SRS coverage
| SRS | Where |
|---|---|
| Login + Teacher/Student fork | `/login`, `/register`, role checks in `app.py` |
| Permanent course + QR join | `/course/new`, `/course/<id>/add-people`, `/join` |
| Tab 1 Attendance (10 min timer, Wi-Fi only, photo + AI headcount, student history) | `/course/<id>/attendance*` |
| Tab 2 Lesson notes (200 chars, anonymous "student", gated start time) | `/course/<id>/notes`, `/lesson/<id>` |
| Tab 3 Polls (10s answer, 5s reveal, review arrows, delete confirm) | `/course/<id>/polls*`, `/poll-run/<id>*` |
| Tab 4 Lecture summaries (modal + download, delete confirm) | `/course/<id>/summaries` |
| Tab 5 Homework (on time / late hours, edit deadline, AI deep review vs peers) | `/course/<id>/homework`, `/box/<id>`, `/submission/<id>` |
| Tab 6 Supplementary | announcements, participants, attendance spreadsheet export |
| Ex2 "main feature": attendance spreadsheet | `/course/<id>/attendance/export.csv` (opens in Excel / Google Sheets) |

## Configuration (env vars)
- `ALLOWED_NETWORKS` – CIDRs counted as school Wi-Fi (default: loopback + private ranges, so the demo works anywhere; set e.g. `192.168.1.0/24` for the real class).
- Put settings in a local `.env` file (copy `.env.example`; it is git-ignored) or set them as real environment variables (hosting dashboard). Real env vars win over `.env`.
- `GEMINI_API_KEY` (+ optional `GEMINI_MODEL`, default `gemini-3.8-flash`, and `GEMINI_FALLBACK_MODEL`, default `gemini-3.6-flash`, empty to disable; free key from https://aistudio.google.com/apikey) – enables real AI: headcount from photo, lecture summary, homework deep review. Without it: local fallbacks (headcount shows "not available", extractive summary, similarity-based review).
- AI privacy: photos, transcripts and homework text are sent to Google. E-mails, phone numbers and 9-digit IDs are masked and photo metadata is dropped (needs Pillow), but names inside the text are not. On Google's free tier people may review this data and it may be used to improve Google's products – use a billed project for real students.
- `SC_DATA` – data folder (DB + uploads). `SECRET_KEY` – session key (change for real use).

## Known limits (demo scope)
- Lecture summaries work from a transcript (txt/vtt/srt, e.g. exported from Zoom/Meet); audio/video is not transcribed.
- No CSRF tokens; use only on a trusted classroom network.

## Tests
    pip install -r requirements-test.txt
    python -m unittest discover -s tests -v

Test plan / design: `docs/` (STP, STD). Files in `tests/`:
- `test_app.py` – original logic tests + Gemini client (the network is mocked; no key needed).
- `test_schedule_and_homework.py` – course hours / overlap rules and homework boxes.
- `test_stp_unit.py` – STP U-01..U-20; `test_stp_logic.py` – new STP logic cases (L-06..L-26);
  `test_stp_permissions.py` – P-01..P-18; `test_std_cases.py` – STD E-01, E-02, N-04..N-06.
- Cases the STP/STD mark "expected to fail today" are `@unittest.expectedFailure`; when a fix
  lands they show as "unexpected success" – remove the decorator then.

Load tests (STD section 7, L-02/L-03): `loadtest/seed_load.py` + `loadtest/locustfile.py`.
