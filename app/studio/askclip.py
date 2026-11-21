"""Ask for a clip in plain words: "clip the part where he says the website is the worst", "the moment he
screams at the car", "Mythpat getting arrested". The AI reads the transcript, finds that moment and the clip is made.

Long videos are too long to send whole, so the AI first turns the request into search words (English and
Hinglish spellings); the stretches of transcript with the most hits are what it reads to pick the exact moment.
Requests with nothing to search for ("the funniest bit") fall back to the loudest-reaction stretches."""
import re

from . import brain

FIND = """You are a video editor's assistant. The person asked for a clip from a stream, in their own words
(English, Hinglish or Hindi):
"{ask}"

Below is the transcript of parts of the stream, each line with its start time in seconds. Find the moment(s) they
mean. Start a little before the key line (the setup), end right after the reaction or payoff.
Each clip is {min_len} to {max_len} seconds unless they asked for another length. Give at most {n} clips; if they
asked for one thing, give one. If nothing in the transcript matches, give no clips and say so in "say".
Video: {title}

<transcript>
{transcript}
</transcript>

Answer as JSON: {{"clips": [{{"start": <seconds>, "end": <seconds>, "title": "<post title, 70 chars max>",
"hook": "<3-5 words, ALL CAPS>", "why": "<8 words: what happens>"}}], "say": "<one short sentence for the person, in English>"}}"""
FIND_SCHEMA = {"type": "OBJECT", "properties": {
    "clips": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "title": {"type": "STRING"}, "hook": {"type": "STRING"},
        "why": {"type": "STRING"}}, "required": ["start", "end"]}},
    "say": {"type": "STRING"}}, "required": ["clips"]}

TERMS = """Someone wants a clip from a stream transcript and described it like this: "{ask}"
List search words that would appear in the transcript at that moment: names, key words, short phrases, in English
AND in Hinglish spellings (Hindi written in English letters, several spellings), no stop words. At most 20.
Answer as JSON: {{"terms": ["..."]}}"""
TERMS_SCHEMA = {"type": "OBJECT", "properties": {"terms": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["terms"]}

WIN = 300           # seconds of transcript per stretch the AI reads
MAX_WIN = 3         # stretches per request (keeps it within a free AI plan's per-minute limit)


def _norm(t):
    return re.sub(r"[^a-z0-9 ]", "", str(t).lower())


def find(words: list, ask: str, title="", min_len=15, max_len=60, loud=None, n=2) -> dict:
    """{"clips": [{start, end, title, hook, why}], "say": "..."} in the words' own time."""
    ask = " ".join(str(ask or "").split())[:300]
    if not ask:
        raise ValueError("Write what you want clipped.")
    if len(words) < 5:
        raise ValueError("There are no words in this video yet to search.")
    t0, t1 = words[0]["s"], words[-1]["e"]
    if t1 - t0 <= WIN * MAX_WIN:
        spans = [(t0, t1)]
    else:
        terms = []
        try:
            out, _m, _t = brain.ask_ai(TERMS.format(ask=brain._safe(ask, 300)), TERMS_SCHEMA, "Ask for a clip: search", max_tokens=300)
            terms = [_norm(x) for x in (out.get("terms") or []) if len(_norm(x)) >= 3][:20]
        except Exception:  # noqa: BLE001
            pass
        terms += [w for w in _norm(ask).split() if len(w) >= 4]
        scores = {}
        for w in words:
            k = int(w["s"] // (WIN / 2))
            tw = _norm(w.get("w", ""))
            if tw and any(t == tw or (len(t) >= 5 and t in tw) or (" " in t and tw in t.split()) for t in terms):
                scores[k] = scores.get(k, 0) + 1
        if not scores and loud is not None and len(loud):             # nothing to search for: the loudest stretches
            import numpy as np
            z = np.asarray(loud, dtype=float)
            for k in range(int(t1 // (WIN / 2)) + 1):
                seg = z[int(k * WIN / 2): int(k * WIN / 2 + WIN)]
                if len(seg):
                    scores[k] = float(np.percentile(seg, 95))
        best = sorted(scores, key=lambda k: -scores[k])[:MAX_WIN] or [0]
        spans = sorted({(max(t0, k * WIN / 2 - WIN / 4), min(t1, k * WIN / 2 + WIN * 0.75)) for k in best})
    lines = []
    for a, b in spans:
        lines += brain.make_lines([w for w in words if a <= w["s"] <= b])
    prompt = FIND.format(ask=brain._safe(ask, 300), min_len=min_len, max_len=max_len, n=n, title=brain._safe(title) or "unknown",
                         transcript=brain._fmt(lines)[:24000])
    out, _m, _t = brain.ask_ai(prompt, FIND_SCHEMA, "Ask for a clip", max_tokens=700)
    clips = []
    for c in (out.get("clips") or []) if isinstance(out, dict) else []:
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e - s < min_len:                  # at least the shortest clip length: a bit more before, the rest after
            s = max(0.0, s - min(3.0, (min_len - (e - s)) / 3))
            e = s + min_len
        e = min(e, s + 180, t1 + 1)
        s = max(0.0, s)
        clips.append({"start": round(s, 2), "end": round(e, 2), "title": brain._text(c.get("title"), 90) if hasattr(brain, "_text") else str(c.get("title", ""))[:90],
                      "hook": str(c.get("hook") or "")[:60].upper(), "why": str(c.get("why") or "")[:120]})
    return {"clips": clips[:n], "say": str(out.get("say") or "")[:200] if isinstance(out, dict) else ""}
