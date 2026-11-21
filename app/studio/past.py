"""The part of a live stream that happened before recording started.

YouTube keeps a running stream from its start (DVR) as numbered 2-second pieces: we fetch just the audio of all of
them (for the transcript) and later only the video pieces that become clips. Twitch saves a running stream as a
VOD that grows while it is live, which is clipped like any video up to the moment recording started."""
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from . import config

WORKERS = 4


def _yt_formats(url):
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "live_from_start": True, **config.yt_cookies()}
    with yt_dlp.YoutubeDL(o) as y:
        i = y.extract_info(url, download=False, process=False)
        for _ in range(3):                 # a channel's /live link first points to the video that is live: follow it
            if i.get("_type") in ("url", "url_transparent") and i.get("url"):
                i = y.extract_info(i["url"], download=False, process=False)
            else:
                break
    fs = [f for f in i.get("formats") or [] if f.get("protocol") == "http_dash_segments_generator" and f.get("url")]
    return i, fs


def find(url: str) -> dict:
    """How to reach the earlier part of this live stream: {kind, url, seconds, title, uploader}, or raises ValueError."""
    from . import media
    if media.is_youtube(url):
        i, fs = _yt_formats(url)
        if not (i.get("is_live") or i.get("live_status") == "is_live"):
            raise ValueError("This stream isn't live.")
        start = i.get("release_timestamp") or i.get("timestamp")
        if not fs or not start:
            raise ValueError("This YouTube stream doesn't keep its earlier part (the streamer turned off rewind).")
        return {"kind": "youtube", "url": media.normalize(i.get("webpage_url") or url), "seconds": max(0, time.time() - float(start)),
                "title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or ""}
    m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", url)
    if m:
        import yt_dlp
        o = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 1}
        with yt_dlp.YoutubeDL(o) as y:
            lst = y.extract_info(f"https://www.twitch.tv/{m.group(1)}/videos?filter=archives&sort=time", download=False)
        e = next(iter(lst.get("entries") or []), None)
        if not e or not e.get("url"):
            raise ValueError("This Twitch channel doesn't save its streams, so the earlier part can't be clipped.")
        vurl = e["url"] if e["url"].startswith("http") else f"https://www.twitch.tv/videos/{e.get('id')}"
        return {"kind": "twitch", "url": vurl, "seconds": float(e.get("duration") or 0), "title": e.get("title") or "",
                "uploader": m.group(1)}
    raise ValueError("Catching up works for YouTube and Twitch streams. Kick doesn't let ClipKar see the earlier part while live.")


def _fetch_run(fmt, start, end, out: Path, on_progress=None, *, complete=False):
    """YouTube pieces covering [start, end) seconds, joined into one file. Returns the seconds actually got."""
    td = float(fmt.get("target_duration") or 2) or 2
    first, last = int(start // td), int(end // td)
    sqs = list(range(first, max(first + 1, last)))
    heads = fmt.get("http_headers") or {}
    got = [None] * len(sqs)
    with httpx.Client(headers=heads, timeout=30, follow_redirects=True) as cl:
        def one(k):
            for n in range(8):                       # YouTube answers 401 when asked too fast: back off and retry
                try:
                    r = cl.get(fmt["url"] + f"&sq={sqs[k]}")
                    if r.status_code == 200 and len(r.content) > 200:
                        got[k] = r.content
                        return
                    if r.status_code in (404, 410):
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.4 * (n + 1))
        done = 0
        with ThreadPoolExecutor(WORKERS) as ex:
            for _ in ex.map(one, range(len(sqs))):
                done += 1
                if on_progress and done % 50 == 0:
                    on_progress(done / len(sqs))
    have = [g for g in got if g]
    if not have or (complete and len(have) != len(sqs)):
        raise RuntimeError("YouTube didn't hand over the earlier part of this stream.")
    if got[0] is None and start < td * 3:
        pass
    with open(out, "wb") as fh:
        for g in got:
            if g:
                fh.write(g)
    return len(have) * td


def _pick_dvr_audio(info: dict, fs: list, audio_track: str = ""):
    from . import media
    auds = [f for f in fs if f.get("vcodec") == "none"]
    if not auds:
        return None
    dvr_info = {**info, "formats": auds}
    selection = media.select_audio_track(dvr_info, audio_track)
    matched = media.filter_audio_formats(auds, selection)
    if not matched:
        return None
    return next((f for f in matched if str(f.get("format_id") or "").split("-")[0] == "140"), None) or matched[0]


def youtube_audio(url: str, seconds: float, out_dir: Path, on_progress=None, start: float = 0.0, audio_track: str = "") -> Path:
    info, fs = _yt_formats(url)
    a = _pick_dvr_audio(info, fs, audio_track=audio_track)
    if not a:
        raise RuntimeError("No audio in the earlier part of this stream.")
    out = out_dir / "audio_src.m4a"
    _fetch_run(a, start, start + seconds, out, on_progress)
    return out


def youtube_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720, *, require_hd=False, expected_id=None, audio_track: str = "") -> Path:
    info, fs = _yt_formats(url)
    if expected_id is not None and str(info.get("id")) != str(expected_id):
        raise RuntimeError("This DVR belongs to a different stream.")
    vids = [f for f in fs if f.get("vcodec") not in (None, "none")]
    if require_hd:
        from . import media
        vids = [f for f in vids if media.has_1080p(f) and (f.get("height") or 0) <= 2160]
        if not vids:
            raise RuntimeError("The DVR has no native 1080p video for this stream.")
        v = min(vids, key=lambda f: (min(f["width"], f["height"]),
                                    not str(f["vcodec"]).startswith("avc1"), -(f.get("fps") or 0)))
    else:
        preferred = [f for f in vids if str(f["vcodec"]).startswith("avc1") and (f.get("height") or 0) <= height]
        v = max(preferred or vids, key=lambda f: (f.get("height") or 0, f.get("fps") or 0))
    a = _pick_dvr_audio(info, fs, audio_track=audio_track)
    if not a:
        raise RuntimeError("No matching audio stream found in the DVR.")
    out_dir.mkdir(parents=True, exist_ok=True)
    vf, af, out = out_dir / f"{stem}.v.mp4", out_dir / f"{stem}.a.mp4", out_dir / f"{stem}.mp4"
    try:
        _fetch_run(v, start, end, vf, complete=require_hd)
        _fetch_run(a, start, end, af, complete=require_hd)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(vf), "-i", str(af),
                        "-c", "copy", "-movflags", "+faststart", str(out)], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        vf.unlink(missing_ok=True)
        af.unlink(missing_ok=True)
    return out


def start_catchup(creator: dict, url: str, min_seconds=240):
    """Start a normal clipping job for the stream so far (called when a recording starts). Returns the job or None."""
    from . import jobs, creators
    p = find(url)
    secs = p["seconds"]
    if secs < min_seconds:
        return None
    cap = min(config.MAX_VIDEO_HOURS, 12) * 3600        # a very long stream: only its latest hours (YouTube keeps ~12 h)
    skip = max(0.0, secs - cap)
    secs = min(secs, cap)
    name = f"{creator['name']}: the stream before recording ({round(secs / 60)} min)"
    if p["kind"] == "youtube":
        src = {"type": "url", "url": p["url"], "name": name, "past": round(secs, 1), "past_from": round(skip, 1)}
        opts = {}
    else:
        src = {"type": "url", "url": p["url"], "name": name, "past": round(secs, 1)}
        opts = {"range_end": round(secs, 1)}
    job = jobs.create(src, opts, creator=creator["id"])
    creators.log(creator["id"], f"Catching up: clipping the {round(secs / 60)} minutes of the stream before recording started")
    return job
