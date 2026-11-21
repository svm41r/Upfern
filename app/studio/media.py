"""Getting video in: link download, probing, audio extraction, loudness."""
import contextlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- when YouTube refuses this server
# YouTube sometimes refuses a server's downloads altogether ("sign in to confirm you're not a bot"). It is a
# refusal of the server, not of one video, so once it happens YouTube links are turned away at once for a few
# minutes, with a clear message, instead of each one waiting to fail. Then the next link simply tries again.
YT_PAUSE = 120     # after YouTube refuses once, its links wait this long before trying again
_yt_refused_until = 0.0
YT_BLOCKED = ("YouTube wants to check you're not a bot. Press Connect YouTube to sign in or verify once, then paste the link again. "
              "Or upload the video file instead, or use a "
              "Twitch or Kick link. Try again in two minutes; if it keeps happening, YouTube has flagged this connection for a few hours.")
_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)", re.I)


class YouTubeBlocked(RuntimeError):
    pass


def is_youtube(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be"))


def youtube_paused() -> bool:
    return time.time() < _yt_refused_until


def youtube_unpause():
    global _yt_refused_until
    _yt_refused_until = 0.0


def check_youtube(url: str):
    """Raise YouTubeBlocked straight away if YouTube is refusing this server right now."""
    if is_youtube(url) and youtube_paused():
        raise YouTubeBlocked(YT_BLOCKED)


def note_refusal(url: str, err) -> bool:
    """If this error is YouTube refusing the server, pause YouTube links for a while. True if it was."""
    global _yt_refused_until
    if is_youtube(url) and _BOT_CHECK.search(str(err)):
        _yt_refused_until = time.time() + YT_PAUSE
        return True
    return False


@contextlib.contextmanager
def _youtube_guard(url):
    check_youtube(url)
    try:
        yield
    except YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if note_refusal(url, e):
            raise YouTubeBlocked(YT_BLOCKED) from None
        raise


def site_allowed(url: str) -> bool:
    """Links are fetched by the server, so only known video sites are accepted (never arbitrary addresses)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in config.ALLOWED_SITES)


def run(cmd, cwd=None, timeout=3600):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(str(cmd[0])).name} took too long and was stopped.")
    if p.returncode != 0:
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed: {p.stderr[-1500:]}")
    return p.stdout


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


_TRY = threading.local()          # how this attempt asks YouTube: with or without the saved sign-in, which player


def _ydl(opts):
    import yt_dlp
    # a channel or playlist link is answered as "a list" straight away, never walked video by video
    base = {"noplaylist": True, "quiet": True, "no_warnings": True, "logger": _Quiet(),
            "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 20, "retries": 5, "fragment_retries": 10,
            "concurrent_fragment_downloads": 4}
    if not getattr(_TRY, "nocookie", False):
        base.update(config.yt_cookies())
    if getattr(_TRY, "client", None):
        base["extractor_args"] = {"youtube": {"player_client": [_TRY.client]}}
    return yt_dlp.YoutubeDL({**base, **opts})


# ------------------------------------------------------------------ links that just work
class LinkError(Exception):
    """A video that can't be opened, with the reason in plain words."""


def normalize(url: str) -> str:
    """Any YouTube link (live/, shorts/, youtu.be, m., music., ?si=...) as a plain watch link; others just tidied."""
    u = str(url or "").strip().strip("<>\"' ")
    if u and "://" not in u:
        u = "https://" + u
    m = re.match(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:live/|shorts/|embed/|v/|watch\?(?:[^#]*&)?v=)|youtu\.be/)([\w-]{11})", u)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else u


_REASONS = [
    (("private video",), "This video is private. Only its owner can see it."),
    (("members-only", "members only", "join this channel"), "This video is for channel members only."),
    (("confirm your age", "age-restricted", "inappropriate for some users"),
     "This video is 18+. Go to Settings, YouTube downloads, press Connect YouTube and sign in with an adult account, then try again."),
    (("live event will begin", "premieres in", "this live stream recording is not available", "will begin in"),
     "This stream or premiere hasn't started yet, or its recording isn't ready. Try again once it's up."),
    (("not made this video available in your country", "not available in your country", "geo restrict", "geo-restrict"),
     "This video is blocked in your country."),
    (("video has been removed", "account associated with this video has been terminated", "video unavailable", "video is unavailable", "this video is no longer available",
      "does not exist", "http error 404"), "This video was removed or the link is wrong. Open it in your browser to check."),
    (("unsupported url",), "That link isn't a video. Copy the link of the video itself."),
    (("getaddrinfo", "failed to resolve", "name or service not known", "network is unreachable", "timed out", "connection reset",
      "connection refused", "connection aborted", "ssl", "certificate", "unable to connect", "remote end closed"),
     "ClipKar can't reach the internet right now. Check your connection (or turn off a VPN or firewall blocking it) and try again."),
    (("http error 429", "too many requests"), "YouTube says too many requests. Wait a few minutes and try again."),
    (("http error 403",), "YouTube refused the download. Press Connect YouTube in Settings, YouTube downloads, then try again."),
]
_OUTDATED = ("requested format is not available", "nsig", "signature", "player response", "unable to extract", "js runtime",
             "javascript runtime", "no supported javascript", "challenge", "failed to extract any player response", "precondition")


def explain(e) -> str:
    low = str(e).lower()
    if any(k in low for k in ("no video formats", "requested format is not available", "js runtime", "javascript runtime", "challenge")):
        from . import jsrt
        if not jsrt.runtime():
            return jsrt.HOW
    return _explain(e)


def _explain(e) -> str:
    """The real reason a link didn't work, in plain words."""
    if isinstance(e, (LinkError, YouTubeBlocked)):
        return str(e)
    t = str(e).lower()
    for keys, text in _REASONS:
        if any(k in t for k in keys):
            return text
    short = re.sub(r"\s+", " ", re.sub(r"^.*?ERROR:\s*(\[[^\]]*\]\s*)?([\w-]{11}:\s*)?", "", str(e))).strip()[:160]
    return "Couldn't open that video" + (f": {short.rstrip('.')}." if short else ".") + " If it opens in your browser, press Try again in a minute."


def _final(e) -> bool:
    """A reason no retry can fix (private, removed, members, region, not started, not a video)."""
    t = str(e).lower()
    return any(k in t for keys, _ in _REASONS[:7] for k in keys)


def _log(url, e):
    try:
        f = config.DATA_DIR / "logs" / "links.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.is_file() and f.stat().st_size > 2_000_000:
            f.write_text("", encoding="utf-8")
        with f.open("a", encoding="utf-8") as h:
            h.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {url}\n{str(e)[-2000:]}\n\n")
    except OSError:
        pass


_UPDATED = {"at": 0.0}


def update_ytdlp() -> bool:
    """Bring the YouTube downloader up to date (YouTube changes often and old versions stop working).
    At most once every 6 hours. True when a newer one was installed: it is used after ClipKar restarts."""
    import importlib.metadata
    stamp = config.DATA_DIR / "logs" / "ytdlp_update.txt"
    try:
        last = float(stamp.read_text().strip()) if stamp.is_file() else 0.0
    except (OSError, ValueError):
        last = 0.0
    if time.time() - max(last, _UPDATED["at"]) < 6 * 3600:
        return False
    _UPDATED["at"] = time.time()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
    except OSError:
        pass
    before = importlib.metadata.version("yt-dlp")
    exe = Path(sys.executable)
    py = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file() else exe
    try:
        subprocess.run([str(py), "-m", "pip", "install", "-U", "--no-input", "--disable-pip-version-check", "yt-dlp[default]"],
                       capture_output=True, timeout=240, creationflags=NO_WINDOW)
        importlib.invalidate_caches()
        after = subprocess.run([str(py), "-c", "import importlib.metadata as m;print(m.version('yt-dlp'))"], capture_output=True,
                               text=True, timeout=30, creationflags=NO_WINDOW).stdout.strip()
    except Exception:  # noqa: BLE001
        return False
    return bool(after) and after != before


def resilient(fn):
    """Open a link several ways before giving up: as is, without the saved YouTube sign-in (an old one breaks things),
    then as other YouTube players. If all fail in a way an update fixes, yt-dlp updates itself."""
    import functools

    @functools.wraps(fn)
    def wrapped(url, *a, **k):
        url = normalize(url)
        tries = [{}]
        if is_youtube(url):
            tries += [{"nocookie": True}, {"client": "tv"}, {"client": "web_safari"}, {"client": "mweb"}]
        last = None
        for t in tries:
            if t.get("nocookie") and not config.yt_cookies():
                continue
            _TRY.nocookie, _TRY.client = t.get("nocookie", False), t.get("client")
            try:
                return fn(url, *a, **k)
            except (YouTubeBlocked, LinkError):
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                _log(url, e)
                if _final(e):
                    raise LinkError(explain(e)) from None
            finally:
                _TRY.nocookie, _TRY.client = False, None
        if last is not None and any(k in str(last).lower() for k in _OUTDATED) and update_ytdlp():
            raise LinkError("YouTube changed something, so ClipKar just updated its downloader. Close ClipKar, open it again, then paste the link again.")
        raise LinkError(explain(last)) from None
    return wrapped


def _video_format(height):
    return f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"


# ------------------------------------------------------------------ audio track & caption language verification
LANG_NAMES = {
    "en": "English", "ar": "Arabic", "hi": "Hindi", "ur": "Urdu", "es": "Spanish", "fr": "French",
    "de": "German", "pt": "Portuguese", "ru": "Russian", "ja": "Japanese", "ko": "Korean", "tr": "Turkish",
    "id": "Indonesian", "tl": "Tagalog", "it": "Italian", "pl": "Polish", "th": "Thai", "vi": "Vietnamese",
    "bn": "Bangla", "zh": "Chinese", "pa": "Punjabi", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
    "mr": "Marathi", "nl": "Dutch", "sv": "Swedish", "uk": "Ukrainian", "fa": "Persian",
}
_WHISPER_LANG_ALIASES = {
    "english": "en", "arabic": "ar", "hindi": "hi", "urdu": "ur", "spanish": "es", "french": "fr",
    "german": "de", "portuguese": "pt", "russian": "ru", "japanese": "ja", "korean": "ko", "turkish": "tr",
    "indonesian": "id", "tagalog": "tl", "italian": "it", "polish": "pl", "thai": "th", "vietnamese": "vi",
    "bengali": "bn", "bangla": "bn", "chinese": "zh", "mandarin": "zh", "punjabi": "pa", "tamil": "ta",
    "telugu": "te", "malayalam": "ml", "marathi": "mr", "dutch": "nl", "swedish": "sv", "ukrainian": "uk",
    "persian": "fa",
    "eng": "en", "ara": "ar", "hin": "hi", "urd": "ur", "spa": "es", "fra": "fr", "fre": "fr",
    "deu": "de", "ger": "de", "por": "pt", "rus": "ru", "jpn": "ja", "kor": "ko", "tur": "tr",
    "ind": "id", "tgl": "tl", "ita": "it", "pol": "pl", "tha": "th", "vie": "vi", "ben": "bn",
    "zho": "zh", "chi": "zh", "pan": "pa", "tam": "ta", "tel": "te", "mal": "ml", "mar": "mr",
    "nld": "nl", "dut": "nl", "swe": "sv", "ukr": "uk", "fas": "fa", "per": "fa",
}
_DUBBED_RE = re.compile(r"\b(?:auto[- ]?)?dub(?:bed)?\b", re.I)
_DESC_RE = re.compile(r"\bdescriptive\b|\baudio description\b", re.I)
_ORIG_RE = re.compile(r"\boriginal\b", re.I)
_DEF_RE = re.compile(r"\bdefault\b", re.I)
_LATIN_LANGS = {"en", "es", "fr", "de", "pt", "it", "tr", "id", "tl", "pl", "vi", "nl", "sv"}


def normalize_lang(code: str) -> str:
    """Normalize language code or Whisper language name to lowercase tag (e.g. 'en-US' -> 'en-us', 'english' -> 'en')."""
    s = str(code or "").strip().lower().replace("_", "-")
    if s.endswith("-orig"):
        s = s[:-5]
    if s in ("", "auto", "und", "unknown", "none"):
        return ""
    if s in _WHISPER_LANG_ALIASES:
        return _WHISPER_LANG_ALIASES[s]
    return s


def base_lang(code: str) -> str:
    """Primary ISO subtag (e.g. 'en-US' -> 'en', 'zh-Hans' -> 'zh')."""
    n = normalize_lang(code)
    if not n:
        return ""
    return n.split("-", 1)[0]


def lang_name(code: str) -> str:
    b = base_lang(code)
    if not b:
        return "Unknown"
    return LANG_NAMES.get(b, b.upper())


class UnverifiedAudioTrackError(LinkError):
    """Multi-track video whose original creator audio track could not be verified automatically."""

    def __init__(self, message: str, tracks: list | None = None):
        super().__init__(message)
        self.tracks = tracks or []
        self.reason = message


def _classify_format_track(fmt: dict) -> dict:
    raw_lang = str(fmt.get("language") or "").strip()
    note = str(fmt.get("format_note") or "")
    lp = fmt.get("language_preference")
    is_desc = (lp == -10) or bool(_DESC_RE.search(note)) or raw_lang.lower().endswith("-desc")
    is_dubbed = (not is_desc) and bool(_DUBBED_RE.search(note))
    is_original = (not is_desc) and (not is_dubbed) and (
        (lp is not None and lp >= 10) or bool(_ORIG_RE.search(note))
    )
    is_default = (not is_desc) and ((lp is not None and lp >= 5) or bool(_DEF_RE.search(note)))
    norm = normalize_lang(raw_lang.removesuffix("-desc") if raw_lang.lower().endswith("-desc") else raw_lang)
    base = base_lang(norm)
    kind = "descriptive" if is_desc else "original" if is_original else "dubbed" if is_dubbed else "standard"
    track_id = f"{base or 'und'}:{kind}" if base or kind != "standard" else "default"
    pretty = lang_name(base) if base else "Default"
    if is_original:
        label = f"{pretty} (original)"
    elif is_dubbed:
        label = f"{pretty} (dubbed)"
    elif is_desc:
        label = f"{pretty} (descriptive)"
    elif is_default:
        label = f"{pretty} (default)"
    else:
        label = pretty
    return {
        "id": track_id,
        "lang": norm,
        "base_lang": base,
        "label": label,
        "is_original": is_original,
        "is_dubbed": is_dubbed,
        "is_default": is_default,
        "is_descriptive": is_desc,
        "language_preference": lp,
    }


def _audio_candidates(info: dict) -> list:
    fmts = info.get("formats") or []
    aud_only = [f for f in fmts if f.get("vcodec") == "none" and (
        f.get("acodec") not in (None, "none") or str(f.get("protocol") or "").startswith("m3u8")
    )]
    if aud_only:
        return aud_only
    return [f for f in fmts if f.get("acodec") not in (None, "none")]


def _consolidate_track_group(group: list, base: str, is_desc: bool, force_kind: str = "") -> dict:
    is_original = (force_kind == "original") or ((force_kind == "") and (not is_desc) and any(t["is_original"] for t in group))
    is_dubbed = (force_kind == "dubbed") or ((force_kind == "") and (not is_desc) and (not is_original) and any(t["is_dubbed"] for t in group))
    is_default = (not is_desc) and any(t["is_default"] for t in group)
    norm = (next((t["lang"] for t in group if t["is_original"] and t["lang"]), "")
            or next((t["lang"] for t in group if t["lang"]), ""))
    kind = "descriptive" if is_desc else "original" if is_original else "dubbed" if is_dubbed else "standard"
    track_id = f"{base or 'und'}:{kind}" if base or kind != "standard" else "default"
    pretty = lang_name(base) if base else "Default"
    if is_original:
        label = f"{pretty} (original)"
    elif is_dubbed:
        label = f"{pretty} (dubbed)"
    elif is_desc:
        label = f"{pretty} (descriptive)"
    elif is_default:
        label = f"{pretty} (default)"
    else:
        label = pretty
    lps = [t["language_preference"] for t in group if t["language_preference"] is not None]
    return {
        "id": track_id,
        "lang": norm,
        "base_lang": base,
        "label": label,
        "is_original": is_original,
        "is_dubbed": is_dubbed,
        "is_default": is_default,
        "is_descriptive": is_desc,
        "language_preference": max(lps) if lps else None,
    }


def inspect_audio_tracks(info: dict) -> dict:
    """Inspect audio tracks on a video and verify which track is the original creator audio."""
    cands = _audio_candidates(info)
    grouped = {}
    for f in cands:
        t = _classify_format_track(f)
        grouped.setdefault((t["base_lang"], t["is_descriptive"]), []).append(t)

    tracks = []
    for (base, is_desc), group in grouped.items():
        has_orig = (not is_desc) and any(t["is_original"] for t in group)
        has_dub = (not is_desc) and any(t["is_dubbed"] for t in group)
        if has_orig and has_dub:
            tracks.append(_consolidate_track_group([t for t in group if t["is_original"]], base, False, force_kind="original"))
            tracks.append(_consolidate_track_group([t for t in group if not t["is_original"]], base, False, force_kind="dubbed"))
        else:
            tracks.append(_consolidate_track_group(group, base, is_desc))
    tracks.sort(key=lambda t: (
        not t["is_original"],
        t["is_dubbed"],
        t["is_descriptive"],
        not t["is_default"],
        t["label"],
    ))
    non_desc = [t for t in tracks if not t["is_descriptive"]]
    lang_bases = {t["base_lang"] for t in non_desc if t["base_lang"]}
    multi_track = len(non_desc) > 1

    if len(non_desc) <= 1 and len(lang_bases) <= 1:
        orig = non_desc[0] if non_desc else None
        orig_lang = (orig["base_lang"] if orig else "") or base_lang(info.get("language") or "")
        return {
            "verified": True,
            "verification": "single_track",
            "original_lang": orig_lang,
            "original_track": orig,
            "tracks": tracks,
            "multi_track": False,
        }

    origs = [t for t in non_desc if t["is_original"]]
    orig_bases = {t["base_lang"] for t in origs if t["base_lang"]}
    if len(orig_bases) == 1:
        orig = origs[0]
        return {
            "verified": True,
            "verification": "original_metadata",
            "original_lang": orig["base_lang"],
            "original_track": orig,
            "tracks": tracks,
            "multi_track": multi_track,
        }

    non_dubs = [t for t in non_desc if not t["is_dubbed"]]
    non_dub_bases = {t["base_lang"] for t in non_dubs if t["base_lang"]}
    if len(non_dub_bases) == 1:
        orig = non_dubs[0]
        return {
            "verified": True,
            "verification": "only_non_dubbed",
            "original_lang": orig["base_lang"],
            "original_track": orig,
            "tracks": tracks,
            "multi_track": multi_track,
        }

    if not origs and not any(t["is_dubbed"] for t in non_desc):
        top_lang = base_lang(info.get("language") or "")
        if top_lang and len(lang_bases) == 1 and top_lang in lang_bases:
            orig = non_desc[0]
            return {
                "verified": True,
                "verification": "single_language_metadata",
                "original_lang": top_lang,
                "original_track": orig,
                "tracks": tracks,
                "multi_track": False,
            }

    return {
        "verified": False,
        "verification": "unverified",
        "original_lang": None,
        "original_track": None,
        "tracks": tracks,
        "multi_track": multi_track,
    }


def format_track_list(tracks: list) -> str:
    shown = [f"{t['label']} [{t['id']}]" for t in tracks if not t.get("is_descriptive")]
    if not shown:
        shown = [f"{t['label']} [{t['id']}]" for t in tracks]
    return ", ".join(shown) if shown else "none detected"


def select_audio_track(info: dict, requested_track: str = "") -> dict:
    """Resolve which audio track to use. Defaults strictly to verified original creator audio."""
    if "formats" not in info and "audio_tracks" in info:
        tracks = info.get("audio_tracks") or []
        orig_lang = info.get("original_audio_lang") or ""
        orig_track = next((t for t in tracks if t.get("is_original") and t.get("base_lang") == orig_lang), None)
        if not orig_track and info.get("original_audio_verified"):
            orig_track = next((t for t in tracks if not t.get("is_descriptive") and (not orig_lang or t.get("base_lang") == orig_lang)), tracks[0] if tracks else None)
        inspection = {
            "verified": bool(info.get("original_audio_verified", True)),
            "verification": info.get("audio_verification") or ("single_track" if not info.get("multi_track") else "original_metadata"),
            "original_lang": orig_lang,
            "original_track": orig_track,
            "tracks": tracks,
            "multi_track": bool(info.get("multi_track")),
        }
    else:
        inspection = inspect_audio_tracks(info)
    req = str(requested_track or "").strip().lower()
    if req in ("", "auto", "original"):
        if not inspection["verified"]:
            avail = format_track_list(inspection["tracks"])
            raise UnverifiedAudioTrackError(
                f"Could not verify the original audio track for this video. Available audio tracks: {avail}. "
                "Select an audio track before clipping.",
                tracks=inspection["tracks"],
            )
        orig = inspection["original_track"]
        return {
            "track": orig,
            "id": orig["id"] if orig else "default",
            "lang": inspection["original_lang"] or "",
            "label": orig["label"] if orig else lang_name(inspection["original_lang"] or ""),
            "is_dubbed": False,
            "is_descriptive": False,
            "verified": True,
            "verification": inspection["verification"],
            "multi_track": inspection["multi_track"],
            "tracks": inspection["tracks"],
            "explicit": False,
        }

    tracks = inspection["tracks"]
    pick = next((t for t in tracks if t["id"].lower() == req), None)
    if not pick:
        req_base = base_lang(req.split(":", 1)[0])
        want_dub = req.endswith(":dubbed")
        want_orig = req.endswith(":original")
        cands = [t for t in tracks if t["base_lang"] == req_base and not t["is_descriptive"]]
        if want_dub:
            cands = [t for t in cands if t["is_dubbed"]] or cands
        elif want_orig:
            cands = [t for t in cands if t["is_original"] or not t["is_dubbed"]] or cands
        else:
            cands.sort(key=lambda t: (not t["is_original"], t["is_dubbed"], not t["is_default"]))
        pick = cands[0] if cands else None
    if not pick:
        raise LinkError(
            f"Requested audio track '{requested_track}' is not available. "
            f"Available audio tracks: {format_track_list(tracks)}."
        )
    return {
        "track": pick,
        "id": pick["id"],
        "lang": pick["base_lang"],
        "label": pick["label"],
        "is_dubbed": pick["is_dubbed"],
        "is_descriptive": pick["is_descriptive"],
        "verified": True,
        "verification": "explicit",
        "multi_track": inspection["multi_track"],
        "tracks": tracks,
        "explicit": True,
    }


def filter_audio_formats(formats: list, selection: dict) -> list:
    """Keep ONLY formats belonging to the selected audio track — never fall back to another dub/language."""
    want_lang = selection.get("lang") or ""
    want_dub = bool(selection.get("is_dubbed"))
    want_desc = bool(selection.get("is_descriptive"))
    multi = bool(selection.get("multi_track"))

    matched = []
    for f in formats:
        t = _classify_format_track(f)
        if t["is_descriptive"] and not want_desc:
            continue
        if want_desc and not t["is_descriptive"]:
            continue
        if want_lang and t["base_lang"] and t["base_lang"] != want_lang:
            continue
        if multi:
            if want_lang and not t["base_lang"]:
                continue
            if want_dub and not t["is_dubbed"] and t["is_original"]:
                continue
            if not want_dub and t["is_dubbed"]:
                continue
        matched.append((f, t))

    if not want_dub:
        orig_only = [(f, t) for f, t in matched if t["is_original"]]
        if orig_only:
            matched = orig_only
    elif want_dub and any(t["is_original"] for _f, t in matched):
        dub_only = [(f, t) for f, t in matched if t["is_dubbed"]]
        if dub_only:
            matched = dub_only
    return [f for f, _ in matched]


def _pick_best_audio_format(formats: list, hls: bool = False) -> dict | None:
    if not formats:
        return None
    if hls:
        def hls_key(f):
            t = _classify_format_track(f)
            base_id = str(f.get("format_id") or "").split("-")[0]
            return (
                1 if t["is_original"] else 0,
                1 if base_id == "234" else 0,
                float(f.get("abr") or f.get("tbr") or 0),
                1 if t["is_default"] else 0,
            )
        return max(formats, key=hls_key)

    def aud_key(f):
        t = _classify_format_track(f)
        fid = str(f.get("format_id") or "")
        is_drc = "-drc" in fid.lower() or "drc" in str(f.get("format_note") or "").lower()
        return (
            1 if t["is_original"] else 0,
            1 if f.get("ext") == "m4a" else 0,
            0 if is_drc else 1,
            float(f.get("abr") or f.get("tbr") or 0),
        )
    return max(formats, key=aud_key)


def _pinned_audio_ids(info: dict, selection: dict, *, hls: bool = False) -> list:
    cands = [f for f in (info.get("formats") or []) if f.get("vcodec") == "none" and (
        f.get("acodec") not in (None, "none") or str(f.get("protocol") or "").startswith("m3u8")
    )]
    if hls:
        cands = [f for f in cands if str(f.get("protocol") or "").startswith("m3u8")]
    matched = filter_audio_formats(cands, selection)
    if not matched:
        return []
    ranked = sorted(matched, key=lambda f: (
        1 if _classify_format_track(f)["is_original"] else 0,
        1 if (hls or not str(f.get("protocol") or "").startswith("m3u8")) else 0,
        1 if str(f.get("format_id") or "").split("-")[0] == "234" else 0,
        1 if f.get("ext") == "m4a" else 0,
        0 if "-drc" in str(f.get("format_id") or "").lower() else 1,
        float(f.get("abr") or f.get("tbr") or 0),
    ), reverse=True)
    return [str(f["format_id"]) for f in ranked if f.get("format_id")]


def _biggest(job_dir: Path, stem: str) -> Path:
    files = sorted((p for p in job_dir.glob(stem + ".*") if p.suffix not in (".part", ".ytdl")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return files[0]


@resilient
def info(url: str) -> dict:
    """Title, channel, length, and verified audio-track metadata, without downloading anything."""
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
    kind = i.get("_type") or "video"
    out = {
        "title": i.get("title") or "",
        "uploader": i.get("uploader") or i.get("channel") or "",
        "duration": float(i.get("duration") or 0),
        "is_live": bool(i.get("is_live")),
        "thumbnail": i.get("thumbnail") or "",
        "kind": kind,
        "language": base_lang(i.get("language") or ""),
    }
    if kind == "video":
        insp = inspect_audio_tracks(i)
        orig = insp["original_track"]
        out.update(
            audio_tracks=insp["tracks"],
            original_audio_verified=insp["verified"],
            original_audio_lang=insp["original_lang"] or "",
            original_audio_label=(orig["label"] if orig else lang_name(insp["original_lang"] or "")) if insp["verified"] else "",
            multi_track=insp["multi_track"],
            audio_verification=insp["verification"],
        )
    return out


_ZW_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]+")


def caption_matches_audio_lang(words: list, expected_lang: str) -> bool:
    """Verify caption script matches the selected audio language so mismatched/translated captions are rejected."""
    base = base_lang(expected_lang)
    if not base or not words:
        return True
    sample = " ".join(str(w.get("w") or "") for w in words[:400])
    latin = arabic = deva = cyrillic = cjk = other = 0
    for ch in sample:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if o < 0x0250 or (0x1E00 <= o <= 0x1EFF):
            latin += 1
        elif (0x0600 <= o <= 0x06FF) or (0x0750 <= o <= 0x077F) or (0xFB50 <= o <= 0xFDFF) or (0xFE70 <= o <= 0xFEFF):
            arabic += 1
        elif 0x0900 <= o <= 0x097F:
            deva += 1
        elif 0x0400 <= o <= 0x052F:
            cyrillic += 1
        elif (0x3040 <= o <= 0x30FF) or (0x4E00 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7AF):
            cjk += 1
        else:
            other += 1
    total = latin + arabic + deva + cyrillic + cjk + other
    if total < 12:
        return True
    if base in _LATIN_LANGS:
        return (latin / total) >= 0.70
    if base == "ar":
        return (arabic / total) >= 0.35
    if base in ("hi", "ur"):
        return ((deva + arabic + latin) / total) >= 0.70
    if base in ("ru", "uk"):
        return (cyrillic / total) >= 0.35
    if base in ("ja", "ko", "zh"):
        return ((cjk + latin) / total) >= 0.35
    return True


def youtube_words(url: str, lang: str = "", audio_lang: str = "", is_dubbed: bool = False):
    """YouTube's own transcript of a video, word by word: ([{'w', 's', 'e'}, ...], language), or None if
    unavailable or mismatched against the selected audio language. Never uses translated/dubbed captions."""
    if not is_youtube(url) or is_dubbed:
        return None
    target = base_lang(audio_lang) or base_lang(lang)

    def _match_orig(group, code):
        if f"{code}-orig" in group:
            return f"{code}-orig"
        return next((k for k in group if k.lower().endswith("-orig") and base_lang(k) == code), None)

    def _match_key(group, code):
        orig = _match_orig(group, code)
        if orig:
            return orig
        if code in group:
            return code
        return next((k for k in group if base_lang(k) == code and not k.lower().endswith("-orig")), None)

    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        subs = i.get("subtitles") or {}
        auto = i.get("automatic_captions") or {}
        top_lang = base_lang(i.get("language") or "")
        group, key = auto, None
        if target:
            k_orig = _match_orig(auto, target)
            if k_orig:
                group, key = auto, k_orig
            elif top_lang == target and (k_auto := _match_key(auto, target)):
                group, key = auto, k_auto
            elif k_sub := _match_key(subs, target):
                group, key = subs, k_sub
        else:
            key = next((k for k in auto if k.lower().endswith("-orig")), None)
            if not key and top_lang:
                key = _match_key(auto, top_lang)
            group = auto
        track = next((f for f in group.get(key) or [] if f.get("ext") == "json3" and f.get("url")), None) if key else None
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    words = _caption_words(json.loads(raw))
    resolved = normalize_lang(key) or target
    if not words or not caption_matches_audio_lang(words, target or resolved):
        return None
    return words, base_lang(resolved) or resolved


def youtube_english_words(url: str):
    """YouTube's English captions of a video spoken in another language: ones a person wrote if there are any,
    otherwise YouTube's own translation of its transcript. Word list like youtube_words, or None."""
    if not is_youtube(url):
        return None
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        tracks = []
        for group in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
            for k in ("en", "en-US", "en-GB"):
                tracks.append(next((f for f in group.get(k) or [] if f.get("ext") == "json3" and f.get("url")), None))
        track = next((t for t in tracks if t), None)
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    words = _caption_words(json.loads(raw))
    return words or None


def _caption_words(data: dict) -> list:
    raw = []
    for ev in data.get("events") or []:
        segs = [x for x in ev.get("segs") or [] if _ZW_RE.sub("", x.get("utf8") or "").strip()]
        if not segs:
            continue
        t0, dur = float(ev.get("tStartMs") or 0), float(ev.get("dDurationMs") or 0)
        timed = len(segs) > 1 or "tOffsetMs" in segs[0]
        for x in segs:
            txt = _ZW_RE.sub("", x["utf8"])
            parts = txt.split()
            for n, w in enumerate(parts):
                if re.fullmatch(r"[\[(].*[\])]", w) or w in ("[", "]", "__"):
                    continue                      # [Music], [Applause], and words YouTube blanked out
                # a line with no word timings: its words are spread evenly over the time the line was on screen
                at = t0 + float(x.get("tOffsetMs") or 0) if timed else t0 + dur * n / max(1, len(parts))
                raw.append({"w": w, "s": round(at / 1000, 2)})
    raw.sort(key=lambda w: w["s"])
    out = []
    for w in raw:
        if out and abs(w["s"] - out[-1]["s"]) < 0.08 and w["w"].lower() == out[-1]["w"].lower():
            continue
        out.append(w)
    for n, w in enumerate(out):
        nxt = out[n + 1]["s"] if n + 1 < len(out) else w["s"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, w["s"] + 0.6)), 2)
    return out


_PREVIEWS: dict = {}


def preview(url: str) -> dict:
    """What the user will get from a link, checked before any work starts. The thumbnail comes back inline
    (the page may only load images from this site). Cached, so retyping a link costs nothing."""
    url = normalize(url)
    if url in _PREVIEWS:
        return _PREVIEWS[url]
    i = info(url)
    if i["kind"] != "video" or not i["title"]:
        raise ValueError("That link is a channel or playlist, not a single video.")
    thumb = ""
    t = i.get("thumbnail") or ""
    if t.startswith("https://"):
        import base64
        import urllib.request
        try:
            req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(600_000 + 1)
            if ctype in ("image/jpeg", "image/png", "image/webp") and len(data) <= 600_000:
                thumb = f"data:{ctype};base64," + base64.b64encode(data).decode()
        except Exception:
            thumb = ""
    out = {
        "title": i["title"][:200],
        "uploader": i["uploader"][:120],
        "duration": i["duration"],
        "is_live": i["is_live"],
        "thumb": thumb,
        "language": i.get("language") or "",
        "audio_tracks": i.get("audio_tracks") or [],
        "original_audio_verified": bool(i.get("original_audio_verified", True)),
        "original_audio_lang": i.get("original_audio_lang") or "",
        "original_audio_label": i.get("original_audio_label") or "",
        "multi_track": bool(i.get("multi_track")),
    }
    if len(_PREVIEWS) > 300:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    _PREVIEWS[url] = out
    return out


def save_thumb(job_dir: Path, url: str = "", video: Path = None) -> bool:
    """A small picture for the video list: the link's own thumbnail, or a frame from an uploaded file."""
    import base64
    out = Path(job_dir) / "clips" / "thumb.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if url:
            p = _PREVIEWS.get(url.strip()) or preview(url)
            if not p.get("thumb"):
                return False
            out.write_bytes(base64.b64decode(p["thumb"].split(",", 1)[1]))
            return True
        if video:
            d = probe(video).get("duration") or 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(3.0, d / 3):.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)])
            return out.is_file()
    except Exception:
        return False
    return False


@resilient
def download_audio(url: str, job_dir: Path, on_progress=None, audio_track: str = "") -> Path:
    """Audio only: pinned strictly to the verified original track (or explicitly chosen track)."""
    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if on_progress and d.get("status") == "downloading" and total:
            on_progress(d.get("downloaded_bytes", 0) / total)

    fmt = "ba[ext=m4a]/ba/worst"
    if is_youtube(url):
        with _youtube_guard(url), _ydl({"ignore_no_formats_error": True}) as ydl:
            i = ydl.extract_info(url, download=False)
        selection = select_audio_track(i, audio_track)
        ids = _pinned_audio_ids(i, selection, hls=False)
        if not ids:
            raise LinkError(f"No downloadable audio format found for {selection['label']}.")
        fmt = "/".join(ids)

    with _youtube_guard(url), _ydl({"format": fmt, "outtmpl": str(job_dir / "audio_src.%(ext)s"),
               "progress_hooks": [hook]}) as ydl:
        ydl.download([url])
    return _biggest(job_dir, "audio_src")


@resilient
def download(url: str, job_dir: Path, height=720, audio_track: str = "") -> Path:
    """The whole video with audio pinned to the verified original track (or explicitly chosen track)."""
    fmt = _video_format(height)
    if is_youtube(url):
        with _youtube_guard(url), _ydl({"ignore_no_formats_error": True}) as ydl:
            i = ydl.extract_info(url, download=False)
        selection = select_audio_track(i, audio_track)
        ids = _pinned_audio_ids(i, selection, hls=False)
        if not ids:
            raise LinkError(f"No downloadable audio format found for {selection['label']}.")
        asel = "/".join(ids)
        fmt = f"bv*[height<={height}][vcodec^=avc1]+({asel})/bv*[height<={height}]+({asel})"
        if not selection["multi_track"]:
            fmt += f"/b[height<={height}]/b"

    with _youtube_guard(url), _ydl({"format": fmt, "merge_output_format": "mp4",
                                    "outtmpl": str(job_dir / "source.%(ext)s")}) as ydl:
        ydl.download([url])
    return _biggest(job_dir, "source")


def _google_media_url(url):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname and parts.hostname.endswith(".googlevideo.com"))


def is_twitch(url):
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    return host == "twitch.tv" or host.endswith(".twitch.tv")


def _twitch_media_url(url):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname and parts.hostname.endswith(
        (".ttvnw.net", ".twitchcdn.net", ".twitch.tv", ".cloudfront.net")))


def _manifest_parts(text, url, start, end, allowed=_google_media_url):
    """Select complete segments with known times from a finished YouTube HLS playlist."""
    from urllib.parse import urljoin

    if "#EXT-X-ENDLIST" not in text or any(tag in text for tag in (
            "#EXT-X-KEY:", "#EXT-X-BYTERANGE:")):
        return []
    when = length = 0.0
    found = []
    init = selected_init = None
    discontinuity = False
    for line in text.splitlines():
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match or "BYTERANGE=" in line:
                return []
            init = urljoin(url, match[1])
            if not allowed(init):
                return []
        elif line == "#EXT-X-DISCONTINUITY":
            discontinuity = True
        elif line.startswith("#EXTINF:"):
            length = float(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and length:
            if when + length > start and when < end:
                segment = urljoin(url, line)
                if not allowed(segment):
                    return []
                if found and (discontinuity or init != selected_init):
                    return []
                if not found:
                    selected_init = init
                    if init:
                        found.append((when, init))
                found.append((when, segment))
            when += length
            length = 0.0
            discontinuity = False
            if when >= end:
                break
    return found


_replay_cache_lock = threading.Lock()
_replay_info_cache = {}
_replay_manifest_cache = {}


def _replay_cached(cache, key, fetch):
    now = time.monotonic()
    with _replay_cache_lock:
        previous = cache.get(key)
        if previous and previous[0] > now:
            return previous[1]
    value = fetch()
    with _replay_cache_lock:
        cache[key] = (time.monotonic() + 180, value)
        if len(cache) > 8:
            cache.pop(min(cache, key=lambda k: cache[k][0]), None)
    return value


def _replay_info(url):
    def fetch():
        with _youtube_guard(url), _ydl({"extract_flat": False}) as ydl:
            return ydl.extract_info(url, download=False, process=False)
    return _replay_cached(_replay_info_cache, url, fetch)


def _fetch_replay_track(fmt, dest, start, end, allowed):
    from concurrent.futures import ThreadPoolExecutor

    with httpx.Client(timeout=15, headers=fmt.get("http_headers") or {}) as client:
        def playlist():
            manifest = client.get(fmt["url"])
            manifest.raise_for_status()
            return manifest.text

        text = _replay_cached(_replay_manifest_cache, fmt["url"], playlist)
        parts = _manifest_parts(text, fmt["url"], start, end, allowed)
        if not parts:
            raise ValueError("No complete segments in the replay timeline.")

        def fetch(item):
            response = client.get(item[1])
            response.raise_for_status()
            if not response.content:
                raise ValueError("Empty replay segment.")
            return response.content

        # Submit in small groups so a slow segment cannot retain the rest of the window in RAM.
        with ThreadPoolExecutor(6) as pool, dest.open("wb") as f:
            for pos in range(0, len(parts), 6):
                for chunk in pool.map(fetch, parts[pos:pos + 6]):
                    f.write(chunk)
        return parts[0][0]


def replay_tracks(url, audio_track: str = ""):
    """Use finished provider playlists only. A live capture has a different clock."""
    allowed = _twitch_media_url if is_twitch(url) else _google_media_url
    info_raw = _replay_info(url)
    if info_raw.get("is_live"):
        return None
    hls = [f for f in info_raw.get("formats", []) if str(f.get("protocol") or "").startswith("m3u8")
           and allowed(f.get("url") or "")]
    audio = [f for f in hls if f.get("vcodec") == "none"]
    muxed = [f for f in hls if f.get("height") and f.get("acodec") != "none"]
    if not audio and not muxed:
        return None
    if is_youtube(url):
        selection = select_audio_track(info_raw, audio_track)
        filtered = filter_audio_formats(audio, selection)
        if not filtered:
            return None
        track = _pick_best_audio_format(filtered, hls=True)
    else:
        track = _pick_best_audio_format(audio, hls=True) if audio else min(
            muxed, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    if not track:
        return None
    return allowed, track, muxed


def replay_audio_window(tracks, start, end, destination):
    from tempfile import TemporaryDirectory

    allowed, track, _ = tracks
    with TemporaryDirectory(prefix="scan-", dir=destination.parent) as temp:
        input_path = Path(temp) / "audio.ts"
        offset = _fetch_replay_track(track, input_path, start, end, allowed)
        run(["ffmpeg", "-v", "error", "-y", "-i", str(input_path), "-ss", str(start - offset),
             "-t", str(end - start), "-vn", "-ac", "1", "-ar", "16000", str(destination)], timeout=60)


def _twitch_hd_segments(url, out_dir, stem, start, end):
    from tempfile import TemporaryDirectory

    tracks = replay_tracks(url)
    if not tracks:
        return None
    allowed, _, muxed = tracks
    choices = [f for f in muxed if has_1080p(f)]
    if not choices:
        return None
    track = min(choices, key=lambda f: (abs(f["height"] - 1080), -(f.get("fps") or 0)))
    with TemporaryDirectory(prefix="twitch-", dir=out_dir) as temp:
        raw, output = Path(temp) / "vod.ts", Path(temp) / "clip.mp4"
        offset = _fetch_replay_track(track, raw, start, end, allowed)
        run(["ffmpeg", "-v", "error", "-y", "-ss", str(start - offset), "-i", str(raw),
             "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-t", str(end - start),
             "-movflags", "+faststart", str(output)], timeout=60)
        meta = probe(output)
        if not has_1080p(meta) or meta["duration"] < end - start - .75:
            return None
        result = out_dir / f"{stem}.mp4"
        output.replace(result)
        return result


def _youtube_hd_segments(url, out_dir, stem, start, end, audio_track: str = ""):
    """Download the selected replay segments directly when both HD video and verified audio have VOD playlists."""
    from concurrent.futures import ThreadPoolExecutor
    from tempfile import TemporaryDirectory

    info_raw = _replay_info(url)
    if info_raw.get("is_live"):
        return None
    selection = select_audio_track(info_raw, audio_track)
    hls = [f for f in info_raw.get("formats", []) if str(f.get("protocol") or "").startswith("m3u8")
           and _google_media_url(f.get("url") or "")]
    hd = [f for f in hls if f.get("height") == 1080 and has_1080p(f)
          and str(f.get("vcodec") or "").startswith("avc1")]
    audio = [f for f in hls if f.get("vcodec") == "none" and (
        str(f.get("format_id") or "").split("-")[0] in ("233", "234")
        or str(f.get("acodec") or "").startswith("mp4a")
    )]
    audio = filter_audio_formats(audio, selection)
    picked_audio = _pick_best_audio_format(audio, hls=True)
    if not hd or not picked_audio:
        return None

    with TemporaryDirectory(prefix="replay-", dir=out_dir) as tmp:
        tmp = Path(tmp)
        vf, af, candidate = tmp / "v.ts", tmp / "a.ts", tmp / "clip.mp4"
        with ThreadPoolExecutor(2) as pool:
            video_task = pool.submit(_fetch_replay_track, max(hd, key=lambda f: f.get("fps") or 0), vf, start, end, _google_media_url)
            audio_task = pool.submit(_fetch_replay_track, picked_audio, af, start, end, _google_media_url)
            v0, a0 = video_task.result(), audio_task.result()
        if abs(v0 - a0) > .15:
            return None
        delta = str(max(0.0, start - v0))
        run(["ffmpeg", "-v", "error", "-y", "-ss", delta, "-i", str(vf),
             "-ss", delta, "-i", str(af), "-map", "0:v:0", "-map", "1:a:0",
             "-c", "copy", "-t", str(end - start), "-movflags", "+faststart", str(candidate)], timeout=45)
        meta = probe(candidate)
        if not has_1080p(meta) or meta["duration"] < end - start - .75:
            return None
        result = out_dir / (stem + ".mp4")
        candidate.replace(result)
        return result


@resilient
def download_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720, *, require_hd=False, audio_track: str = "") -> Path:
    """Just one stretch of the video, pinned to the verified original audio track (or explicit track)."""
    from yt_dlp.utils import download_range_func
    out_dir.mkdir(parents=True, exist_ok=True)
    if require_hd and (is_youtube(url) or is_twitch(url)):
        try:
            with _youtube_guard(url):
                segmented = (_twitch_hd_segments(url, out_dir, stem, max(0.0, start), end) if is_twitch(url)
                             else _youtube_hd_segments(url, out_dir, stem, max(0.0, start), end, audio_track=audio_track))
            if segmented:
                return segmented
        except (YouTubeBlocked, LinkError):
            raise
        except (RuntimeError, ValueError, OSError, httpx.HTTPError):
            pass  # try the provider player format and FFmpeg's ranged fetch

    if is_youtube(url):
        with _youtube_guard(url), _ydl({"ignore_no_formats_error": True}) as ydl:
            i = ydl.extract_info(url, download=False)
        selection = select_audio_track(i, audio_track)
        hls_ids = _pinned_audio_ids(i, selection, hls=True)
        any_ids = _pinned_audio_ids(i, selection, hls=False)
        if not hls_ids and not any_ids:
            raise LinkError(f"No downloadable audio stream found for {selection['label']}.")
        hsel = "/".join(hls_ids or any_ids)
        asel = "/".join(any_ids or hls_ids)
        if require_hd:
            formats = (f"bv*[protocol^=m3u8][height=1080][width>=1080][vcodec^=avc1]+({hsel})/"
                       f"bv*[protocol^=m3u8][height=1080][width>=1080]+({hsel})/"
                       f"bv*[height=1080][width>=1080][vcodec^=avc1]+({asel})/"
                       f"bv*[height=1080][width>=1080]+({asel})/"
                       f"bv*[width=1080][height>=1080]+({asel})/"
                       f"bv*[height>1080][width>=1080][height<=2160]+({asel})")
            if not selection["multi_track"]:
                formats += ("/b[protocol^=m3u8][height=1080][width>=1080]/"
                            "b[height=1080][width>=1080]/b[width=1080][height>=1080]/"
                            "b[height>1080][width>=1080][height<=2160]")
        else:
            formats = (f"bv*[protocol^=m3u8][height<={height}][vcodec^=avc1]+({hsel})/"
                       f"bv*[protocol^=m3u8][height<={height}]+({hsel})/"
                       f"bv*[height<={height}][vcodec^=avc1]+({asel})/"
                       f"bv*[height<={height}]+({asel})")
            if not selection["multi_track"]:
                formats += f"/b[protocol^=m3u8][height<={height}]/b[height<={height}]/b"
    else:
        # The streaming (m3u8) versions come in small segments, so only the few that hold this stretch are fetched,
        # in seconds. The single-file versions are a last resort: YouTube slows ranged reads of those to a crawl.
        hls = (f"bv*[protocol^=m3u8][height<={height}][vcodec^=avc1]+ba[protocol^=m3u8]/"
               f"bv*[protocol^=m3u8][height<={height}]+ba[protocol^=m3u8]/b[protocol^=m3u8][height<={height}]/")
        formats = hls + _video_format(height)
        if require_hd:
            # Landscape 1080p, vertical 1080p, or a higher native resolution.
            formats = ("bv*[protocol^=m3u8][height=1080][width>=1080][vcodec^=avc1]+ba[protocol^=m3u8]/"
                       "bv*[protocol^=m3u8][height=1080][width>=1080]+ba[protocol^=m3u8]/"
                       "b[protocol^=m3u8][height=1080][width>=1080]/"
                       "bv*[height=1080][width>=1080][vcodec^=avc1]+ba/"
                       "bv*[height=1080][width>=1080]+ba/b[height=1080][width>=1080]/"
                       "bv*[width=1080][height>=1080]+ba/b[width=1080][height>=1080]/"
                       "bv*[height>1080][width>=1080][height<=2160]+ba/b[height>1080][width>=1080][height<=2160]")
    with _youtube_guard(url), _ydl({"format": formats, "merge_output_format": "mp4",
               "outtmpl": str(out_dir / (stem + ".%(ext)s")),
               "download_ranges": download_range_func(None, [(max(0.0, start), end)]),
               # ffmpeg does the ranged fetch and has no network timeout of its own: a stalled googlevideo
               # connection (common on live replays) would otherwise hang the job forever
               "external_downloader_args": {"ffmpeg_i": ["-rw_timeout", "30000000", "-reconnect", "1",
                                                         "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]},
               }) as ydl:
        ydl.download([url])
    return _biggest(out_dir, stem)


def find_offset(full: np.ndarray, part: np.ndarray, expected: float, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded piece really begins, by matching the sound itself."""
    n = min(len(part), 30 * 16000)
    a0 = max(0, int((expected - search) * 16000))
    hay = full[a0: int((expected + search) * 16000) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if n < 16000 or len(hay) < n or float(np.abs(needle).mean()) < 20:
        return expected  # too short or silent to match: trust the requested time
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / 16000


MIN_CLIP_RESOLUTION = 1080


def has_1080p(meta: dict) -> bool:
    return min(meta.get("width") or 0, meta.get("height") or 0) >= MIN_CLIP_RESOLUTION


def match_audio(full: np.ndarray, part: np.ndarray, expected: float, search=20.0):
    """Return a verified offset in seconds, or None when different audio could fit."""
    if len(part) < 16000:
        return None
    t = find_offset(full, part, expected, search)
    i = round(t * 16000)
    a, b = full[i:i + len(part)].astype(np.float32), part.astype(np.float32)
    if len(a) != len(b):
        return None
    a -= a.mean()
    b -= b.mean()
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return t if norm > 1 and float(np.dot(a, b)) / norm >= .65 else None


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    astreams = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if not astreams:
        raise RuntimeError("This file has no audio track, so there is nothing to transcribe.")
    w, h = int(v["width"]), int(v["height"])
    if w * h > 3840 * 2160 or w < 16 or h < 16:
        raise RuntimeError("This video's resolution isn't supported (4K is the largest).")
    rot = str((v.get("side_data_list") or [{}])[0].get("rotation", "0"))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    tracks = []
    for idx, s in enumerate(astreams):
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        lang = base_lang(tags.get("language") or "")
        title = str(tags.get("title") or "").strip()
        is_orig = bool(disp.get("original")) or bool(_ORIG_RE.search(title))
        is_dub = bool(disp.get("dub")) or bool(_DUBBED_RE.search(title))
        is_def = bool(disp.get("default"))
        tracks.append({
            "audio_index": idx,
            "lang": lang,
            "title": title,
            "is_original": is_orig,
            "is_dubbed": is_dub,
            "is_default": is_def,
            "label": (title or lang_name(lang) or f"Track {idx + 1}"),
        })
    pick = next((t for t in tracks if t["is_original"] and not t["is_dubbed"]), None)
    if not pick:
        non_dub = [t for t in tracks if not t["is_dubbed"]]
        if len(non_dub) == 1:
            pick = non_dub[0]
        else:
            pick = next((t for t in non_dub if t["is_default"]), non_dub[0] if non_dub else tracks[0])
    return {
        "width": w,
        "height": h,
        "duration": float(data["format"]["duration"]),
        "audio_tracks": tracks,
        "audio_stream_index": pick["audio_index"],
        "audio_lang": pick["lang"],
    }


def extract_audio(src: Path, wav: Path, stream_index: int | None = None):
    # first_pts=0 pads any gap before the audio starts, so second 0 of the wav is second 0 of the file
    # -t: never more audio than the longest video allowed, whatever the file claims about itself
    amap = f"0:a:{int(stream_index)}" if stream_index is not None else "0:a:0"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", amap, "-vn", "-ac", "1", "-ar", "16000",
         "-t", str(int(config.MAX_VIDEO_HOURS * 3600) + 60),
         "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(wav)])


def load_audio(wav: Path, mapped: bool = False) -> np.ndarray:
    """The 16 kHz audio, read from the file as it is used instead of all held in memory (a 24-hour stream is
    2.7 GB of audio). Works like a normal array: slices read just their part."""
    import struct
    if not mapped:      # a small piece: simply read it (the file can then be deleted straight away)
        data, sr = sf.read(str(wav), dtype="int16")
        assert sr == 16000
        return data
    with open(wav, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            data, sr = sf.read(str(wav), dtype="int16")
            return data
        pos, sr, bits, start = 12, 16000, 16, None
        while start is None:
            f.seek(pos)
            h = f.read(8)
            if len(h) < 8:
                raise ValueError("No audio in that file.")
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(16)
                sr, bits = struct.unpack("<I", fmt[4:8])[0], struct.unpack("<H", fmt[14:16])[0]
            elif cid == b"data":
                start = pos + 8
            pos += 8 + size + (size & 1)
    assert sr == 16000 and bits == 16
    n = (Path(wav).stat().st_size - start) // 2
    return np.memmap(wav, dtype="<i2", mode="r", offset=start, shape=(n,))


def loudness_per_second(audio: np.ndarray) -> np.ndarray:
    """RMS level in dB for each second of audio."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    out = []
    for a in range(0, n, 600):                    # ten minutes at a time, so a long stream never fills the memory
        b = min(n, a + 600)
        x = np.asarray(audio[a * 16000: b * 16000]).reshape(b - a, 16000).astype(np.float32) / 32768.0
        out.append(np.sqrt((x ** 2).mean(axis=1) + 1e-10))
    rms = np.concatenate(out)
    return (20 * np.log10(rms)).astype(np.float32)


def audio_features_per_second(audio: np.ndarray) -> dict:
    """Per-second loudness (dB), z-score, and sudden jump/spike above recent baseline."""
    db = loudness_per_second(audio)
    n = len(db)
    if n < 5:
        return {"db": db, "z": np.zeros(n, dtype=np.float32), "spike": np.zeros(n, dtype=np.float32)}
    z = ((db - np.median(db)) / (db.std() + 1e-6)).astype(np.float32)
    spike = np.zeros(n, dtype=np.float32)
    for i in range(1, n):
        base = float(np.median(db[max(0, i - 4):i]))
        spike[i] = max(0.0, float(db[i]) - base)
    return {"db": db, "z": z, "spike": spike}


def visual_activity_per_second(video_path: Path, max_seconds: float = 7200) -> np.ndarray:
    """Lightweight 1fps frame-change intensity curve for multimodal scene/reaction cues. Optional best-effort."""
    from tempfile import TemporaryDirectory
    if not video_path or not Path(video_path).is_file():
        return np.zeros(0, dtype=np.float32)
    try:
        with TemporaryDirectory(prefix="vis-") as temp:
            raw = Path(temp) / "frames.bin"
            run(["ffmpeg", "-y", "-v", "error", "-t", str(int(max_seconds)), "-i", str(video_path),
                 "-vf", "fps=1,scale=96:54,format=gray", "-an", "-f", "rawvideo", str(raw)], timeout=45)
            data = np.fromfile(raw, dtype=np.uint8)
        nframes = len(data) // (96 * 54)
        if nframes < 3:
            return np.zeros(0, dtype=np.float32)
        frames = data[:nframes * 96 * 54].reshape(nframes, 54, 96).astype(np.int16)
        diff = (np.abs(np.diff(frames, axis=0)) >= 17).mean(axis=(1, 2)).astype(np.float32)
        return np.concatenate([np.zeros(1, dtype=np.float32), diff])
    except Exception:  # noqa: BLE001 - visual cues enhance ranking, never fail a job
        return np.zeros(0, dtype=np.float32)
