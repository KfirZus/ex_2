"""AI helpers. Uses the Google Gemini API (free tier) when GEMINI_API_KEY is set (plain HTTPS,
no extra packages needed); otherwise falls back to simple local heuristics so the demo still runs."""
import base64
import difflib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# used in order when MODEL is overloaded / out of quota / times out (comma separated, empty = none)
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash,gemini-3.5-flash-lite")
RETRY_DELAYS = (2, 5, 10)  # seconds to wait before each retry on temporary Google errors
RETRY_CODES = (429, 500, 502, 503, 504)
TIMEOUT = 35          # seconds to wait for each answer of the last model
QUICK_TIMEOUT = 20    # ... and of the models before it
COOLDOWN = 120        # seconds to put a model last after it was overloaded or did not answer
LONG_COOLDOWN = 3600  # seconds to skip one whose daily quota is used up, or that no longer exists
TOTAL_BUDGET = 75     # seconds for one AI request in total (all retries + the fallback model)
MIN_TRY = 5           # do not start another attempt with less time than this left
MAX_IMAGE_SIDE = 2048  # photos are shrunk to this many pixels before being sent
MAX_IMAGE_BYTES = 14 * 1024 * 1024  # Gemini's inline limit is 20 MB, and base64 adds a third
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
CUT_OFF_NOTE = "\n\n[התשובה נקטעה כי הגיעה לאורך המרבי]"


def ai_enabled():
    return bool(API_KEY)


class _ModelUnavailable(RuntimeError):
    """Overload / rate limit / timeout / unknown model: worth trying another model."""

    def __init__(self, message, cooldown=0, quota=False):
        super().__init__(message)
        self.cooldown = cooldown  # seconds to leave this model alone
        self.quota = quota        # Google says our request quota is used up


_down_until = {}  # model -> time.monotonic() until which it is skipped


def _quota_info(detail):
    """(seconds Google asks us to wait or None, True if the daily quota is used up)."""
    try:
        details = json.loads(detail)["error"].get("details") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, False
    wait, daily = None, False
    for d in details:
        if not isinstance(d, dict):
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", "")))
        if m:
            wait = float(m.group(1))
        for v in d.get("violations") or []:
            if isinstance(v, dict) and "PerDay" in str(v.get("quotaId", "")):
                daily = True
    return wait, daily


def _http_failure(e, attempt):
    """(error, wait): what went wrong, and the seconds to wait before trying the same model again
    (None = do not)."""
    detail = e.read().decode("utf-8", "replace")
    message = "Gemini HTTP %s: %s" % (e.code, detail[:400])  # includes Google's explanation
    if e.code == 404:  # model name retired / unknown
        return _ModelUnavailable(message, LONG_COOLDOWN), None
    if e.code not in RETRY_CODES:
        return RuntimeError(message), None  # bad key etc.: another model would not help
    wait, daily = _quota_info(detail)
    header = (e.headers.get("Retry-After") or "") if e.headers else ""
    if wait is None and header.isdigit():
        wait = float(header)
    quota = e.code == 429
    if daily:
        return _ModelUnavailable(message, LONG_COOLDOWN, quota), None
    cooldown = (wait or 60) if quota else max(wait or 0, COOLDOWN)
    if attempt >= len(RETRY_DELAYS):
        return _ModelUnavailable(message, cooldown, quota), None
    return (_ModelUnavailable(message, cooldown, quota),
            RETRY_DELAYS[attempt] if wait is None else wait)


def _request(model, body, deadline, timeout, retry):
    """retry: try the same model again after a temporary error (only worth it for the last model:
    failed attempts use up the small free quota, and another model usually answers at once)."""
    req = urllib.request.Request(
        API_URL % model, data=body,
        headers={"x-goog-api-key": API_KEY, "content-type": "application/json"})
    attempt = 0
    while True:
        left = deadline - time.monotonic()
        if left < MIN_TRY:
            raise _ModelUnavailable("no time left for another attempt")
        try:
            with urllib.request.urlopen(req, timeout=min(timeout, left)) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            error, wait = _http_failure(e, attempt)
        except (OSError, http.client.HTTPException) as e:  # timeout / dropped connection
            error = _ModelUnavailable("no answer from Gemini (%s)" % e, COOLDOWN)
            wait = 2 if attempt == 0 else None
        if wait is None or not retry or time.monotonic() + wait + MIN_TRY > deadline:
            raise error
        time.sleep(wait)
        attempt += 1


def _models():
    """Models to try, in order. Ones that failed a moment ago go last; ones that will be down for
    long (daily quota, unknown model) are skipped, unless there is nothing else."""
    names = [MODEL] + [f.strip() for f in FALLBACK_MODEL.split(",")]
    names = list(dict.fromkeys(n for n in names if n))
    now = time.monotonic()
    wait = {n: max(0, _down_until.get(n, 0) - now) for n in names}
    names.sort(key=wait.get)  # stable: available models keep their order
    return [n for n in names if wait[n] <= COOLDOWN] or names


def _call(system, content, max_tokens=1500, partial_ok=True, quick=QUICK_TIMEOUT):
    """content: a string, or a list of parts: {"text": ...} / {"image": (mime, base64)}.
    partial_ok: return a cut-off answer with a visible note; otherwise treat it as a failure.
    quick: seconds to wait for a model before moving on to the next one."""
    if isinstance(content, str):
        content = [{"text": content}]
    parts = [{"inlineData": {"mimeType": p["image"][0], "data": p["image"][1]}}
             if "image" in p else {"text": p["text"]} for p in content]
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        # the limit also covers the model's internal "thinking" tokens, so leave headroom
        "generationConfig": {"maxOutputTokens": max_tokens + 2048},
    }).encode()
    models = _models()
    deadline = time.monotonic() + TOTAL_BUDGET
    out_of_quota = False
    for i, model in enumerate(models):
        last = i == len(models) - 1
        try:
            # the last model always keeps enough time for one full attempt
            data = _request(model, body, deadline - (0 if last else TIMEOUT),
                            TIMEOUT if last else quick, retry=last)
            break
        except _ModelUnavailable as e:
            out_of_quota = out_of_quota or e.quota
            if e.cooldown:
                _down_until[model] = time.monotonic() + e.cooldown
            if last:
                e.quota = out_of_quota
                raise
            print("[AI] %s unavailable (%s) - trying %s" % (model, e, models[i + 1]), file=sys.stderr)
    cands = data.get("candidates") or []
    text = "".join(p.get("text", "") for c in cands[:1]
                   for p in (c.get("content") or {}).get("parts", []))
    reason = cands[0].get("finishReason") if cands else (
        (data.get("promptFeedback") or {}).get("blockReason"))
    if not text.strip():
        raise RuntimeError("Gemini returned no text (%s)" % (reason or "empty response"))
    if reason == "MAX_TOKENS":
        if not partial_ok:
            raise RuntimeError("Gemini answer was cut off (MAX_TOKENS)")
        text += CUT_OFF_NOTE
    return text


def _scrub(text):
    """Hides e-mails, phone numbers and 9-digit ID numbers before text leaves the server."""
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[אימייל]", text)
    text = re.sub(r"(?<![\w.])(?:\+972[- ]?|0)\d{1,2}[- ]?\d{3}[- ]?\d{4}(?!\w)", "[טלפון]", text)
    return re.sub(r"(?<![\w.])\d{9}(?!\w|\.\d)", "[מספר מזהה]", text)


# ---------------------------------------------------------------- headcount
def _load_photo(path):
    """(mime, bytes) to send: shrunk and re-encoded, which also drops EXIF data such as GPS.
    Without Pillow, or if it cannot read the file, the original bytes are sent."""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))  # fast, low-memory JPEG decoding
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=85, exif=b"")
        return "image/jpeg", out.getvalue()
    except FileNotFoundError:
        raise
    except Exception as e:
        print("[AI] photo not resized:", e, file=sys.stderr)
    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise ValueError("photo is too large to send")
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        return mime, f.read()


def headcount(image_path):
    """(number of people, None) or (None, Hebrew explanation for the teacher)."""
    if not ai_enabled():
        return None, None
    try:
        mime, data = _load_photo(image_path)
        out = _call(
            "You count people in classroom photos. Reply with a single integer only.",
            [{"image": (mime, base64.b64encode(data).decode())},
             {"text": "How many people are visible in this photo?"}], 20, partial_ok=False, quick=15)
        m = re.search(r"\d+", out)
        if m:
            return int(m.group()), None
        print("[AI] headcount failed: no number in the answer", file=sys.stderr)
    except _ModelUnavailable as e:
        print("[AI] headcount failed:", e, file=sys.stderr)
        if e.quota:
            return None, "הספירה לא בוצעה: מכסת הבקשות ל-Google נוצלה. המתינו כדקה ונסו שוב."
        return None, "הספירה לא בוצעה: שירות ה-AI של Google עמוס או לא זמין. נסו שוב בעוד כמה דקות."
    except Exception as e:
        print("[AI] headcount failed:", e, file=sys.stderr)
    return None, "הספירה נכשלה. פרטי השגיאה מופיעים בחלון השרת."


def count_people(image_path):
    """Returns an int, or None when AI is unavailable / fails."""
    return headcount(image_path)[0]


# ------------------------------------------------------------------ summary
# First line of a stand-in summary made while the AI was down; such summaries are never saved.
AI_FAILED_NOTE = "שירות ה-AI אינו זמין כרגע. זהו סיכום מקומי זמני; נסו שוב מאוחר יותר."


def is_failed_summary(summary):
    # the second prefix is what earlier versions saved in the database on an AI error
    return summary.startswith((AI_FAILED_NOTE, "שגיאה בקריאה ל-AI"))


def summarize_lecture(text):
    if not text.strip():
        return "לא התקבל תמלול להרצאה. יש להעלות קובץ תמלול (txt/vtt/srt) או סיכום של Zoom/Meet."
    if ai_enabled():
        try:
            return _call("אתה עוזר הוראה. סכם הרצאות בעברית בצורה ברורה: נושאים עיקריים, "
                         "מושגים חשובים ונקודות לסיכום, בבולטים קצרים.", _scrub(text[:60000]))
        except Exception as e:
            print("[AI] summary failed:", e, file=sys.stderr)
            return "%s\n\n%s" % (AI_FAILED_NOTE, _extractive(text))
    return _extractive(text)


def _extractive(text, n=7):
    """Frequency-based extractive summary (fallback)."""
    text = re.sub(r"\d+\s*\n\d\d:\d\d:\d\d[,.]\d+ --> .*\n", " ", text)  # srt/vtt stamps
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 25]
    if len(sents) <= n:
        return "\n".join("• " + s for s in sents) or "הטקסט קצר מדי לסיכום."
    words = re.findall(r"\w{3,}", text.lower())
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    scored = sorted(range(len(sents)), key=lambda i: -sum(
        freq.get(w, 0) for w in re.findall(r"\w{3,}", sents[i].lower())) / (len(sents[i]) ** 0.5))
    keep = sorted(scored[:n])
    return "סיכום אוטומטי (ללא AI חיצוני):\n" + "\n".join("• " + sents[i] for i in keep)


# ----------------------------------------------------------------- homework
def _tokens(t):
    return set(re.findall(r"\w{3,}", t.lower()))


def similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0
    seq = difflib.SequenceMatcher(None, a[:4000], b[:4000]).ratio()
    return round(100 * (0.5 * jac + 0.5 * seq))


def review_homework(mine_name, mine_text, others):
    """others: list of (label, text). Returns Hebrew review text."""
    sims = sorted(((lbl, similarity(mine_text, t)) for lbl, t in others), key=lambda x: -x[1])
    if ai_enabled():
        try:
            peers = "\n\n".join("=== עבודה %d ===\n%s" % (i + 1, _scrub(t[:4000]))
                                for i, (_, t) in enumerate(others[:12]))
            return _call(
                "אתה בודק עבודות בית מנוסה. כתוב בעברית סקירה מעמיקה: חוזקות, חולשות, "
                "הצעות לשיפור, והשוואה לעבודות האחרות (מקוריות, רמה יחסית, חפיפות חשודות). "
                "אל תחשוף שמות של סטודנטים אחרים.",
                "העבודה הנבדקת:\n%s\n\nעבודות אחרות להשוואה:\n%s" % (_scrub(mine_text[:8000]), peers),
                2000)
        except Exception as e:
            print("[AI] homework review failed:", e, file=sys.stderr)
    lines = ["סקירה אוטומטית (ללא AI חיצוני):",
             "• אורך העבודה: %d מילים" % len(mine_text.split()),
             "• הושוו %d עבודות אחרות." % len(others)]
    if sims:
        avg = sum(s for _, s in sims) / len(sims)
        lines.append("• דמיון ממוצע לעבודות האחרות: %d%%" % avg)
        lines.append("• הדמיון הגבוה ביותר: %d%%%s" % (
            sims[0][1], " — מומלץ לבדוק חפיפה" if sims[0][1] >= 60 else ""))
    else:
        lines.append("• אין עדיין עבודות אחרות להשוואה.")
    return "\n".join(lines)
