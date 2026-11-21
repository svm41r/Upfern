"""What the person wants the AI to clip ("mostly reactions, skip gameplay talk"). Kept in data/ai_focus.json and
copied onto each video or recording when it starts; a live recording's copy can be changed while it runs."""
import json

from . import config

PRESETS = ["Mostly reactions: the streamer reacting big to something (shock, laughing, screaming, disbelief)",
           "Funny moments and banter", "Rage and fails", "Clutch plays and hype moments", "Drama, arguments and roasts",
           "Wholesome or emotional moments"]


def _file():
    return config.DATA_DIR / "ai_focus.json"


def get() -> str:
    try:
        return str(json.loads(_file().read_text(encoding="utf-8")).get("text", ""))[:400]
    except (OSError, ValueError):
        return ""


SCHEMA = {"type": "OBJECT", "properties": {"english": {"type": "STRING"}}, "required": ["english"]}


def understand(text: str) -> str:
    """Hinglish ("zyada reactions clip karo"), Hindi or Urdu script, or English in; one clear English instruction out.
    Plain English is kept as typed. If the AI can't be reached, the words are used as they are."""
    text = " ".join(str(text or "").split())[:400]
    if not text:
        return ""
    plain = all(ord(ch) < 128 for ch in text)
    hinglish = {"karo", "kro", "zyada", "jyada", "wala", "wale", "mat", "nahi", "nhi", "sirf", "bas", "jab", "jo", "hai", "ho",
                "aur", "ko", "ki", "ka", "ke", "mein", "me", "accha", "acha", "bohot", "bahut", "dikhao", "chahiye", "lena", "do"}
    if plain and not (set(text.lower().replace(",", " ").replace(".", " ").split()) & hinglish):
        return text
    from . import brain
    prompt = ("Someone typed what they want an AI video clipper to clip from a stream. It may be Hinglish (Hindi in Roman "
              "letters), Hindi or Urdu script, or mixed. Rewrite it as ONE short, clear instruction in plain English, "
              "keeping every wish (what to clip, what to skip, names). Do not add anything.\n\nThey typed: " + text +
              '\n\nAnswer as JSON: {"english": "<the instruction>"}')
    try:
        out, _m, _t = brain.ask_ai(prompt, SCHEMA, "AI instruction", max_tokens=120)
        eng = " ".join(str((out or {}).get("english", "")).split())[:400] if isinstance(out, dict) else ""
        return eng or text
    except Exception:  # noqa: BLE001
        return text


def put(text: str) -> str:
    text = understand(text)
    _file().write_text(json.dumps({"text": text}), encoding="utf-8")
    return text
