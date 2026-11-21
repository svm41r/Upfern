"""Meme sounds: search myinstants.com, keep the sounds you use on this PC, and drop them into clips.

Nothing is shipped with ClipKar: sounds are fetched from myinstants when someone searches or uses them, and kept in
data/sfx. "My pack" is the sounds this PC uses for automatic meme sounds, each with a mood (laugh, fail, boom...).
It starts with a classic starter pack, fetched the first time it is needed. When a clip is made with meme sounds
on, the AI reads the clip's words and picks a few moments and a mood for each; a sound of that mood from the pack
goes there. Sounds are mixed quietly under the voice (-12 dB by default) and can be moved, changed or removed."""
import html
import json
import random
import re
import threading
from pathlib import Path

import httpx

from . import config

SITE = "https://www.myinstants.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ClipKar"}
PATH_RE = re.compile(r"^/media/sounds/[\w\-.()%]+\.mp3$")
MOODS = ("boom", "laugh", "fail", "roast", "sad", "hype", "awkward", "wow", "rizz", "confused", "drama")
ORIGINALS = (
    ("Shock Hit", "shock-hit", "boom"),
    ("Tape Stop", "tape-stop", "awkward"),
    ("Fail Drop", "fail-drop", "fail"),
    ("Glitch Pop", "glitch-pop", "confused"),
    ("Clutch Chime", "clutch-chime", "hype"),
    ("Dread Rise", "dread-rise", "drama"),
)
STARTER = [
    ("Vine boom", "/media/sounds/vine-boom.mp3", "boom"),
    ("Metal pipe", "/media/sounds/metal-pipe-clang.mp3", "boom"),
    ("Laugh track", "/media/sounds/laugh-track-2-mp3cut.mp3", "laugh"),
    ("SpongeBob fail", "/media/sounds/spongebob-fail.mp3", "fail"),
    ("Oh no no no", "/media/sounds/oh-no-no-no-no-laugh.mp3", "fail"),
    ("Emotional damage", "/media/sounds/emotional-damage-meme.mp3", "roast"),
    ("Sad violin", "/media/sounds/meme-violin-sad-violin.mp3", "sad"),
    ("Airhorn", "/media/sounds/dj-airhorn-sound-effect.mp3", "hype"),
    ("Crickets", "/media/sounds/awkward-silence-crickets-chirping-meme.mp3", "awkward"),
    ("Bruh", "/media/sounds/bruh-sound-effect_WstdzdM.mp3", "awkward"),
    ("Anime wow", "/media/sounds/anime-wow-sound-effect.mp3", "wow"),
    ("Rizz", "/media/sounds/rizz-sound-effect.mp3", "rizz"),
    ("Huh cat", "/media/sounds/huh-cat-meme.mp3", "confused"),
    ("Dun dun dun", "/media/sounds/dun-dun-dun-sound-effect-brass_8nFBccR.mp3", "drama"),
    ("Taco Bell bong", "/media/sounds/death-bong.mp3", "drama"),
]
_lock = threading.RLock()


def folder() -> Path:
    d = config.DATA_DIR / "sfx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_for(path: str) -> str:
    """The local file name for a myinstants sound path."""
    return re.sub(r"[^\w\-.]", "_", path.rsplit("/", 1)[-1])[:120]


def local(name: str):
    """A kept sound's file, or None. Only plain names inside data/sfx."""
    if not re.fullmatch(r"[\w\-.]{1,120}\.mp3", name or ""):
        return None
    f = folder() / name
    return f if f.is_file() else None


# ------------------------------------------------------------------ myinstants
def search(q: str, page: int = 1) -> list:
    r = httpx.get(f"{SITE}/en/search/", params={"name": q[:60], "page": max(1, min(20, page))}, headers=UA, timeout=20)
    r.raise_for_status()
    out, seen = [], set()
    for m in re.finditer(r"play\('(/media/sounds/[^']+?\.mp3)'.*?class=\"instant-link[^\"]*\">([^<]+)</a>", r.text, re.S):
        path, name = m.group(1), html.unescape(m.group(2)).strip()
        if PATH_RE.match(path) and path not in seen:
            seen.add(path)
            out.append({"name": name[:80], "path": path, "file": file_for(path), "kept": bool(local(file_for(path)))})
    return out[:60]


def fetch(path: str) -> Path:
    """Download a myinstants sound into data/sfx (once). Small mp3 files only."""
    if not PATH_RE.match(path or ""):
        raise ValueError("That isn't a myinstants sound.")
    f = folder() / file_for(path)
    if f.is_file() and f.stat().st_size > 1000:
        return f
    r = httpx.get(SITE + path, headers=UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    data = r.content
    if len(data) > 3_000_000 or len(data) < 1000 or not (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise ValueError("That sound couldn't be downloaded.")
    f.write_bytes(data)
    return f


# ------------------------------------------------------------------ my pack and settings
def _pack_file():
    return folder() / "pack.json"


def pack() -> list:
    with _lock:
        try:
            return json.loads(_pack_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []


def _save_pack(p):
    _pack_file().write_text(json.dumps(p, indent=1), encoding="utf-8")


def ensure_starter() -> list:
    """The starter pack, fetched the first time it's needed (sounds that fail to download are skipped)."""
    with _lock:
        p = pack()
        if p:
            return p
        for name, path, mood in STARTER:
            try:
                fetch(path)
                p.append({"name": name, "file": file_for(path), "path": path, "mood": mood})
            except Exception:  # noqa: BLE001
                continue
        _save_pack(p)
        return p


def pack_add(path: str, name: str, mood: str) -> list:
    fetch(path)
    with _lock:
        p = [x for x in pack() if x["file"] != file_for(path)]
        p.append({"name": name[:80] or file_for(path), "file": file_for(path), "path": path, "mood": mood if mood in MOODS else "boom"})
        _save_pack(p)
        return p


def library() -> list:
    sounds = {item["file"]: item for item in ensure_starter()}
    for name, slug, mood in ORIGINALS:
        file = f"upfern-{slug}.mp3"
        if (folder() / file).is_file():
            sounds.setdefault(file, {"name": name, "file": file, "mood": mood})
    return list(sounds.values())


def pack_remove(file: str) -> list:
    with _lock:
        p = [x for x in pack() if x["file"] != file]
        _save_pack(p)
        return p


def settings() -> dict:
    try:
        s = json.loads((folder() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return {"auto": bool(s.get("auto", False)) if s.get("v") == 2 else False, "db": max(-30, min(0, int(s.get("db", -12)))), "per_clip": max(1, min(5, int(s.get("per_clip", 3))))}


def save_settings(**kw) -> dict:
    s = settings()
    s.update({k: v for k, v in kw.items() if k in ("auto", "db", "per_clip")}, v=2)
    (folder() / "settings.json").write_text(json.dumps(s), encoding="utf-8")
    return settings()


# ------------------------------------------------------------------ the AI picks the moments
SCHEMA = {"type": "OBJECT", "properties": {"picks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "t": {"type": "NUMBER"}, "mood": {"type": "STRING"}}, "required": ["t", "mood"]}}}, "required": ["picks"]}


def auto_pick(words: list, start: float, end: float, title: str = "", only=None) -> list:
    """Up to N meme sounds for a clip: [{name, file, t, db}] (t in seconds of the source)."""
    from . import brain
    st = settings()
    p = library()
    if only:                       # the person ticked some sounds: the AI places only those
        p = [s for s in p if s["file"] in set(only)] or p
    if not p:
        return []
    said = [w for w in words if start <= w["s"] <= end]
    if len(said) < 6:
        return []
    lines, cur = [], []
    for w in said:
        cur.append(w)
        if len(cur) >= 8 or str(w.get("w", "")).endswith((".", "!", "?")):
            lines.append(f"[{cur[0]['s'] - start:.1f}s] " + " ".join(str(x.get("w", "")) for x in cur))
            cur = []
    if cur:
        lines.append(f"[{cur[0]['s'] - start:.1f}s] " + " ".join(str(x.get("w", "")) for x in cur))
    moods = sorted({x["mood"] for x in p})
    names = "; ".join(f"{x['name']} ({x['mood']})" for x in p[:30])
    prompt = f"""You add meme sound effects to a short clip, like a funny TikTok/Shorts editor. Video: {brain._safe(title) or 'unknown'}.
The words said, with seconds from the start of the clip:
{chr(10).join(lines)[:6000]}

Pick at most {st['per_clip']} moments where a meme sound makes it funnier: right AFTER a punchline, a fail, a roast, a shock,
an awkward silence, a hype moment. Not on top of important words, not in the first 1.5 seconds, at least 4 seconds apart.
Fewer is better than forced: if nothing fits, pick nothing. Moods you can use: {", ".join(moods)}.
The sounds available, with their mood: {names}. Match the mood to the scene.
Answer as JSON: {{"picks": [{{"t": <seconds from clip start>, "mood": "<one mood>"}}]}}"""
    try:
        out, _m, _t = brain.ask_ai(prompt, SCHEMA, "Clip: meme sounds", max_tokens=300)
    except Exception:  # noqa: BLE001 - no AI: no automatic sounds
        return []
    res, last = [], -99.0
    for x in sorted((out.get("picks") or []) if isinstance(out, dict) else [], key=lambda x: float(x.get("t", 0) or 0)):
        try:
            t = float(x["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < 1.2 or t > end - start - 0.3 or t - last < 3.5:
            continue
        options = [s for s in p if s["mood"] == x.get("mood")] or [s for s in p if s["mood"] == "boom"] or p
        s = random.choice(options)
        res.append({"name": s["name"], "file": s["file"], "t": round(start + t, 2), "db": 0})
        last = t
        if len(res) >= st["per_clip"]:
            break
    return res
