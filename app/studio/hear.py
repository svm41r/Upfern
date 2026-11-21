"""Captions heard again by Whisper (large-v3, on Groq) for just the clip being made or full non-English audio.

The built-in speech model only knows English, so non-English or multilingual speech needs Whisper when Groq is
configured. Hindi in Devanagari is turned into Roman letters (Hinglish) only when the spoken language is Hindi/Urdu
or Devanagari is present — Arabic, English, Spanish, and other languages are preserved in their native script."""
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from . import ai, config, media

MODEL = "whisper-large-v3"
_off_until = {"t": 0.0}       # after a "limit reached" answer, don't ask again for a while


def ready() -> bool:
    import time
    return bool(ai.groq_key()) and time.time() > _off_until["t"]


def _whisper_post(audio_path: Path, title: str = "", lang: str = "", use_prompt: bool = True) -> dict:
    import time
    sel = media.base_lang(lang)
    data = {
        "model": MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
        "temperature": "0",
    }
    if sel and sel in config.LANGUAGES and sel != "auto":
        data["language"] = sel
    if use_prompt:
        if sel in ("hi", "ur"):
            # an example in the style wanted: Hindi is then written down as said (Hinglish), not translated into English
            data["prompt"] = "Haan bhai, kya scene hai? Ignore karo yaar, ye website bekaar hai. Chal bhai, stream pe aate hain."
        elif sel == "en":
            # Avoid injecting noisy ALL-CAPS clickbait titles which can bias Whisper into hallucinations.
            data["prompt"] = "Clean verbatim English transcript including slang."
    with open(audio_path, "rb") as fh:
        r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=120,
                       headers={"Authorization": "Bearer " + ai.groq_key()},
                       files={"file": (audio_path.name, fh, "audio/flac")},
                       data=data)
    if r.status_code == 429:
        _off_until["t"] = time.time() + 600
    r.raise_for_status()
    return r.json()


def _parse_whisper_words(d: dict, offset_sec: float) -> tuple[list, str]:
    out = []
    for w in d.get("words") or []:
        t = str(w.get("word", "")).strip()
        if not t:
            continue
        s = round(max(0.0, float(w["start"]) + offset_sec), 2)
        e = round(max(s + 0.06, float(w["end"]) + offset_sec), 2)
        if out and s < out[-1]["s"]:
            s = out[-1]["s"]
            e = max(e, round(s + 0.06, 2))
        out.append({"w": t, "s": s, "e": e})
    if not out and (d.get("text") or "").strip():
        raise RuntimeError("Whisper gave no word times")
    det = media.normalize_lang(d.get("language") or "")
    return out, det


def clip_words(src: Path, a: float, b: float, shift: float, title: str = "", lang: str = "",
               expected_lang: str = "", stream_index: int | None = None) -> tuple:
    """Words said in src between a and b seconds (file time), as [{w, s, e}] in job time (file time + shift),
    and the normalized language Whisper heard ("en", "ar", "hi"...). Raises on any failure."""
    sel = media.base_lang(lang)
    exp = media.base_lang(expected_lang)
    base = max(0.0, float(a))
    dur = max(1.0, float(b) - base)
    amap = f"0:a:{int(stream_index)}" if stream_index is not None else "0:a:0"
    with tempfile.TemporaryDirectory(prefix="ckh") as tmp:
        f = Path(tmp) / "a.flac"
        if base > 3.0:
            cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{base - 2.0:.3f}", "-i", str(src), "-ss", "2.000",
                   "-t", f"{dur:.3f}", "-map", amap, "-vn", "-ac", "1", "-ar", "16000",
                   "-af", "aresample=async=1:first_pts=0", "-c:a", "flac", str(f)]
        else:
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ss", f"{base:.3f}",
                   "-t", f"{dur:.3f}", "-map", amap, "-vn", "-ac", "1", "-ar", "16000",
                   "-af", "aresample=async=1:first_pts=0", "-c:a", "flac", str(f)]
        subprocess.run(cmd, check=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        used_prompt = sel in ("en", "hi", "ur")
        d = _whisper_post(f, title=title, lang=sel, use_prompt=True)
        out, det = _parse_whisper_words(d, base + shift)
        sparse_floor = min(10, max(5, int(dur * 0.45)))
        if used_prompt and len(out) < sparse_floor:
            d_np = _whisper_post(f, title="", lang=sel, use_prompt=False)
            out_np, det_np = _parse_whisper_words(d_np, base + shift)
            if len(out_np) > len(out) and (not exp or not out or media.caption_matches_audio_lang(out_np, exp)):
                out, det = out_np, det_np
        if sel and len(out) < sparse_floor:
            d_au = _whisper_post(f, title="", lang="", use_prompt=False)
            out_au, det_au = _parse_whisper_words(d_au, base + shift)
            if len(out_au) > len(out) and (not exp or media.caption_matches_audio_lang(out_au, exp)):
                out, det = out_au, det_au
        # Guard against sparse/screaming/wrong-script auto-detect results when track language is known.
        if not sel and exp and (len(out) < sparse_floor or (len(out) <= 8 and not media.caption_matches_audio_lang(out, exp))):
            d2 = _whisper_post(f, title="", lang=exp, use_prompt=False)
            out2, det2 = _parse_whisper_words(d2, base + shift)
            if out2 and (
                not out
                or (not media.caption_matches_audio_lang(out, exp) and media.caption_matches_audio_lang(out2, exp))
                or (len(out2) > len(out) and media.caption_matches_audio_lang(out2, exp))
            ):
                return out2, sel or det2 or exp
    return out, sel or det


def transcribe_audio_chunks(audio: np.ndarray, lang: str = "", title: str = "", on_progress=None) -> tuple[list, str]:
    """Transcribe a full 16kHz mono int16 audio array with Groq Whisper in ~3-minute chunks."""
    sr = 16000
    chunk_sec, max_sec = 175, 200
    n = len(audio)
    if n < 1600:
        return [], media.base_lang(lang)
    hop = 800
    frames = n // hop
    x = np.asarray(audio[:frames * hop]).reshape(frames, hop).astype(np.float32)
    rms = np.sqrt((x ** 2).mean(axis=1))
    cuts = []
    pos = 0
    while pos < n:
        if n - pos <= max_sec * sr:
            cuts.append((pos, n))
            break
        a_f = (pos + int(chunk_sec * sr)) // hop
        b_f = (pos + int(max_sec * sr)) // hop
        cut = (a_f + int(np.argmin(rms[a_f:b_f]))) * hop + hop // 2
        cuts.append((pos, cut))
        pos = cut
    words = []
    langs = []
    with tempfile.TemporaryDirectory(prefix="ckw") as tmp:
        for idx, (a_i, b_i) in enumerate(cuts):
            seg = np.asarray(audio[a_i:b_i], dtype=np.int16)
            if len(seg) < 4800 or float(np.sqrt(((seg.astype(np.float32) / 32768.0) ** 2).mean())) < 0.003:
                if on_progress:
                    on_progress((idx + 1) / len(cuts))
                continue
            p = Path(tmp) / f"c{idx}.flac"
            sf.write(str(p), seg, sr, format="FLAC")
            d = _whisper_post(p, title=title, lang=lang)
            cw, cdet = _parse_whisper_words(d, a_i / sr)
            words.extend(cw)
            if cdet:
                langs.append(cdet)
            if on_progress:
                on_progress((idx + 1) / len(cuts))
    words.sort(key=lambda w: w["s"])
    det_lang = media.base_lang(lang) or (max(set(langs), key=langs.count) if langs else "")
    return words, det_lang


FIX = """These are caption words from a stream where people mix Hindi and English (Hinglish). A machine wrote
the Hindi parts in Devanagari and then turned them into English letters, so English words said inside Hindi
sentences came out misspelled (vebsait, ignor, streem, phaltu stays phaltu). Fix ONLY the spelling:
- an English word written badly -> its normal English spelling (website, ignore, stream, bro, game, chat)
- a Hindi word -> keep it in simple, common Hinglish spelling (bhai, kya, nahi, kuchh, hai, yaar)
Never translate, never merge or split words, never add or drop words. Same number of words, same order.
Words: {words}
Answer as JSON: {{"words": ["...", "..."]}}"""
FIX_SCHEMA = {"type": "OBJECT", "properties": {"words": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["words"]}


GAME = """Below are the caption lines of a clip from a video or stream, with numbers.
The main creator/channel is {who}.
ONLY return line numbers that are 100% scripted in-game NPCs, game cutscenes, or in-game radio announcements.
CRITICAL RULES:
- NEVER remove co-hosts, friends, group members, Discord callers, teammates, contestants, or group banter.
- NEVER remove the creator reacting to or repeating what someone said.
- If multiple real people are talking/bantering together, return [].
- If unsure, leave the line out. Most clips have none ([]).
Lines:
{lines}
Answer as JSON: {{"game": [<numbers>]}}"""
GAME_SCHEMA = {"type": "OBJECT", "properties": {"game": {"type": "ARRAY", "items": {"type": "INTEGER"}}}, "required": ["game"]}

_GROUP_NON_GAME_RE = re.compile(
    r"\b(podcast|interview|sidemen|ndl|beta squad|try not to laugh|challenge|100 hours|hide and seek|dating|mukbang|irl|vlog|react(?:s|ing)? to)\b",
    re.I,
)


def streamer_only(words: list, who: str = "", title: str = "", is_gaming: bool | None = None) -> list:
    """Drop clearly scripted in-game NPC/cutscene dialogue while preserving all real people & group banter."""
    from . import brain
    if is_gaming is False or len(words) < 12:
        return words
    if is_gaming is None and title and _GROUP_NON_GAME_RE.search(title):
        return words
    lines = brain.make_lines(words)
    if len(lines) < 3:
        return words
    try:
        got, _m, _t = brain.ask_ai(GAME.format(who=brain._safe(who) or "the creator",
                                               lines="\n".join(f"{i}. {l['text']}" for i, l in enumerate(lines))[:6000]),
                                   GAME_SCHEMA, "Captions: streamer only", max_tokens=300)
        game = {int(i) for i in (got.get("game") or []) if str(i).lstrip("-").isdigit()}
    except Exception:  # noqa: BLE001
        return words
    game = {i for i in game if 0 <= i < len(lines)}
    if not game or len(game) > len(lines) * 0.35:      # never strip group conversations or bulk speech
        return words
    cut = [(lines[i]["s"] - 0.05, lines[i]["e"] + 0.05) for i in game]
    kept = [w for w in words if not any(a <= w["s"] <= b for a, b in cut)]
    if len(kept) < max(4, int(len(words) * 0.50)):
        return words
    return kept


def tidy_mixed(words_raw: list, lang: str = "") -> list:
    """Hindi/Urdu Devanagari -> Roman letters (Hinglish). Preserves Arabic, English, Spanish, and other scripts intact."""
    import re as _re
    from . import brain, roman
    sel = media.base_lang(lang)
    has_deva = any(_re.search("[\u0900-\u097f]", w.get("w", "")) for w in words_raw)
    if not has_deva and sel not in ("hi", "ur"):
        return words_raw
    out = roman.words(words_raw)
    hit = [i for i, w in enumerate(words_raw) if _re.search("[\u0900-\u097f\u0600-\u06ff]", w.get("w", ""))]
    if not hit:
        return out
    try:
        for a in range(0, len(out), 120):                     # in batches, so long clips fit
            chunk = out[a:a + 120]
            got, _m, _t = brain.ask_ai(FIX.format(words=[w["w"] for w in chunk]), FIX_SCHEMA, "Captions: Hinglish spelling", max_tokens=1500)
            new = got.get("words") if isinstance(got, dict) else None
            if isinstance(new, list) and len(new) == len(chunk):
                for w, t in zip(chunk, new):
                    t = str(t).strip()
                    if t and len(t) <= 30 and not _re.search("[\u0900-\u097f\u0600-\u06ff]", t):
                        w["w"] = t
    except Exception:  # noqa: BLE001 - the plain Roman version is used
        pass
    return out
