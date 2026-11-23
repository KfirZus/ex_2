"""AI helpers. Uses the Google Gemini API (free tier) when GEMINI_API_KEY is set (plain HTTPS,
no extra packages needed); otherwise falls back to simple local heuristics so the demo still runs."""
import base64
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# used automatically when MODEL is overloaded / times out (set to empty to disable)
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
RETRY_DELAYS = (2, 5, 10)  # seconds to wait before each retry on temporary Google errors
RETRY_CODES = (429, 500, 502, 503, 504)
NETWORK_RETRIES = 1   # extra attempts after a timeout / connection error
TIMEOUT = 90          # seconds to wait for each answer
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"


def ai_enabled():
    return bool(API_KEY)


class _Temporary(RuntimeError):
    """Overload / rate limit / timeout: worth trying another model."""


def _temporary(e):
    return isinstance(e, _Temporary)


def _request(model, body):
    req = urllib.request.Request(
        API_URL % model, data=body,
        headers={"x-goog-api-key": API_KEY, "content-type": "application/json"})
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:  # include Google's explanation (bad key, quota...)
            if e.code in RETRY_CODES and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])  # overloaded / rate limited: try again
                continue
            detail = e.read().decode("utf-8", "replace")[:400]
            cls = _Temporary if e.code in RETRY_CODES else RuntimeError
            raise cls("Gemini HTTP %s: %s" % (e.code, detail)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # timeout / dropped connection: one more try, then give up with a clear message
            if attempt < NETWORK_RETRIES:
                time.sleep(2)
                continue
            raise _Temporary("no answer from Gemini (%s)" % e) from None


def _call(system, content, max_tokens=1500):
    """content: a string, or a list of parts: {"text": ...} / {"image": (mime, base64)}."""
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
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != MODEL else [])
    for i, model in enumerate(models):
        try:
            data = _request(model, body)
            break
        except RuntimeError as e:
            if i == len(models) - 1 or not _temporary(e):
                raise
            print("[AI] %s unavailable (%s) - trying %s" % (model, e, models[i + 1]), file=sys.stderr)
    cands = data.get("candidates") or []
    text = "".join(p.get("text", "") for c in cands[:1]
                   for p in (c.get("content") or {}).get("parts", []))
    if not text.strip():
        reason = (cands[0].get("finishReason") if cands else
                  (data.get("promptFeedback") or {}).get("blockReason")) or "empty response"
        raise RuntimeError("Gemini returned no text (%s)" % reason)
    return text


# ---------------------------------------------------------------- headcount
def count_people(image_path):
    """Returns an int, or None when AI is unavailable / fails."""
    if not ai_enabled():
        return None
    try:
        ext = image_path.rsplit(".", 1)[-1].lower()
        media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                 "webp": "image/webp"}.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        out = _call(
            "You count people in classroom photos. Reply with a single integer only.",
            [{"image": (media, b64)},
             {"text": "How many people are visible in this photo?"}], 20)
        m = re.search(r"\d+", out)
        return int(m.group()) if m else None
    except Exception as e:
        print("[AI] headcount failed:", e, file=sys.stderr)
        return None


# ------------------------------------------------------------------ summary
def summarize_lecture(text):
    if not text.strip():
        return "לא התקבל תמלול להרצאה. יש להעלות קובץ תמלול (txt/vtt/srt) או סיכום של Zoom/Meet."
    if ai_enabled():
        try:
            return _call("אתה עוזר הוראה. סכם הרצאות בעברית בצורה ברורה: נושאים עיקריים, "
                         "מושגים חשובים ונקודות לסיכום, בבולטים קצרים.", text[:60000])
        except Exception as e:
            return "שגיאה בקריאה ל-AI: %s\n\n%s" % (e, _extractive(text))
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
            peers = "\n\n".join("=== עבודה %d ===\n%s" % (i + 1, t[:4000])
                                for i, (_, t) in enumerate(others[:12]))
            return _call(
                "אתה בודק עבודות בית מנוסה. כתוב בעברית סקירה מעמיקה: חוזקות, חולשות, "
                "הצעות לשיפור, והשוואה לעבודות האחרות (מקוריות, רמה יחסית, חפיפות חשודות). "
                "אל תחשוף שמות של סטודנטים אחרים.",
                "העבודה הנבדקת:\n%s\n\nעבודות אחרות להשוואה:\n%s" % (mine_text[:8000], peers),
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
