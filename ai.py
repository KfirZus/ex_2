"""AI helpers. Uses the Claude API when ANTHROPIC_API_KEY is set (plain HTTPS, no extra
packages needed); otherwise falls back to simple local heuristics so the demo still runs."""
import base64
import difflib
import json
import os
import re
import urllib.request

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def ai_enabled():
    return bool(API_KEY)


def _call(system, content, max_tokens=1500):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    return "".join(b.get("text", "") for b in data.get("content", []))


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
            [{"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
             {"type": "text", "text": "How many people are visible in this photo?"}], 20)
        m = re.search(r"\d+", out)
        return int(m.group()) if m else None
    except Exception:
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
            pass
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
