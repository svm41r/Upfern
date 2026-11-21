"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import json
import os
import math
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import brain, config, creators, ctx, load, media, render, roman, safe, transcribe, usage

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
on_clip_ready: list = []   # callbacks (job, clip), run once when a clip first finishes

DEFAULTS = {"max_clips": 18, "min_len": 15, "max_len": 60, "style": "bounce", "layout": "blur", "quality": 720,
            "lang": "auto", "cap_lang": "roman", "audio_track": "", "range_start": None, "range_end": None, "mass": False,
            "auto_cut": False}


LIMITS = {"max_clips": (1, 120), "min_len": (5, 120), "max_len": (10, 180), "quality": (360, 1080)}


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if (k not in DEFAULTS and k not in ("fill_requested", "fast_render", "aspect", "quick_replay", "gameplay_check")) or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        elif k == "aspect":
            v = v if v in render.ASPECTS else "9:16"
        elif k == "lang":
            code = str(v).strip().lower()
            v = code if code in config.LANGUAGES else "auto"
        elif k == "cap_lang":
            v = "english" if str(v).strip().lower() == "english" else "roman"
        elif k == "audio_track":
            v = re.sub(r"[^a-z0-9_:\-]", "", str(v).strip().lower())[:40]
        elif k == "auto_cut":
            v = v is True
        elif k in ("mass", "fill_requested", "fast_render", "quick_replay", "gameplay_check"):
            v = bool(v)
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    return out


def _fmt_clock(sec: float) -> str:
    sec, ms = divmod(max(0, round(float(sec or 0) * 1000)), 1000)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    clock = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{clock}.{ms:03d}".rstrip("0") if ms else clock


def _fmt_span(start: float, end: float) -> str:
    return f"{_fmt_clock(start)} – {_fmt_clock(end)}"


def _attach_clip_meta(job: dict, clip: dict, words_hint: list | None = None, whisper_lang: str = ""):
    """Ensure every clip carries source timestamps, duration, detected language, and a crisp one-line reason."""
    s, e = float(clip.get("start") or 0), float(clip.get("end") or 0)
    clip["source_timestamps"] = _fmt_span(s, e)
    clip["duration"] = round(max(0.0, e - s), 1)
    if job.get("audio_track"):
        clip["audio_track"] = job["audio_track"]
    if job.get("audio_stream_index") is not None and (not clip.get("src") or clip.get("audio_stream_index") is None):
        clip["audio_stream_index"] = job["audio_stream_index"]
    reason = brain._clean_reason(clip.get("reason") or "")
    if not reason and (clip.get("critic") or {}).get("note"):
        reason = brain._clean_reason(clip["critic"]["note"])
    if not reason:
        reason = "Strong standalone moment with clear setup and payoff."
    clip["reason"] = reason
    audio_lang = job.get("audio_lang") or job.get("lang") or ""
    if words_hint:
        dlang = brain.detect_language_info(words_hint, audio_lang=audio_lang, whisper_lang=whisper_lang)
        clip["detected_language"] = dlang["label"]
        clip["detected_lang_code"] = dlang["code"]
    elif not clip.get("detected_language"):
        code = media.base_lang(whisper_lang) or media.base_lang(audio_lang)
        clip["detected_lang_code"] = code
        clip["detected_language"] = media.lang_name(code) if code else "Auto-detected"


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover")


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = [{k: v for k, v in c.items() if k not in ("src", "fixed_words", "fixed_for", "track_keys", "track_for")} for c in job["clips"]]
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(d / "job.json")


class Cancelled(Exception):
    """The owner pressed Cancel. Raised from the next progress update, so the work stops at a safe point."""


def _set(job, **kw):
    with _lock:
        job.update(kw)
    save(job)
    if job.get("cancel"):
        raise Cancelled()


def cancel(jid) -> bool:
    """Ask a waiting or running video to stop. It stops at its next step; clips already finished are kept."""
    job = _jobs.get(jid)
    if not job or job.get("status") not in ("queued", "running"):
        return False
    with _lock:
        job.update(cancel=True, stage="Cancelling")
    save(job)
    return True


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("status") in ("queued", "running") and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status="queued", stage="Picking up where it stopped", error=None, resume=True)
    if job.get("status") == "live":
        job.update(status="done", stage="Recording paused by a server update", was_live=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for c in job.get("clips", []):
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        if c.get("status") in ("rendering", "queued"):
            if buffered and not c.get("src"):
                c["status"] = "queued"  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = "queued"  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = "queued"  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status="error", error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if j["kind"] == "video" and j["status"] in ("queued", "running"))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c.get(k, DEFAULTS[k]) for k in ("max_clips", "min_len", "max_len", "style", "layout", "lang", "cap_lang")}}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    d.mkdir(parents=True)
    opts = {**base, **clean_options(options)}
    if opts["min_len"] >= opts["max_len"]:
        opts["min_len"] = max(5, opts["max_len"] - 5)
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "lang": "" if opts["lang"] == "auto" else opts["lang"], "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {}}
    try:
        from . import focus as _focus
        job["focus"] = _focus.get()
    except Exception:  # noqa: BLE001
        job["focus"] = ""
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid):
    with _lock:
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def _clean_mask(v):
    """One cut-out as the editor sent it, checked and rounded, or None."""
    ok = (isinstance(v, dict) and isinstance(v.get("src"), (list, tuple)) and len(v["src"]) == 4
          and all(isinstance(x, (int, float)) and math.isfinite(x) for x in list(v["src"]) + [v.get("x", 0.5), v.get("y", 0.25), v.get("w", 0.5)]))
    if not ok or v["src"][2] < 0.04 or v["src"][3] < 0.04:
        return None
    return {"src": [round(min(1.0, max(0.0, float(x))), 4) for x in v["src"]],
            "x": round(min(1.0, max(0.0, float(v.get("x", 0.5)))), 4), "y": round(min(1.0, max(0.0, float(v.get("y", 0.25)))), 4),
            "w": round(min(1.0, max(0.08, float(v.get("w", 0.5)))), 4),
            "shape": v.get("shape") if v.get("shape") in render.MASK_SHAPES else "round", "border": bool(v.get("border"))}


def request_render(jid, cid, changes: dict):
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    if changes.get("latest"):          # the clip card's 🔄: made again with today's defaults
        clip.update(layout="facegame", hook_seam=False, hook_pos=None, cap_pos=None,
                    hook_nobox=None, fixed_for=None, sfx=clip.get("sfx") or [])
        if not clip.get("words_edited"):
            clip.pop("fixed_words", None)
        h = clip.get("hook") or ""
        # an old short hook ("WAIT FOR IT") is written again as a headline naming the streamer; a headline gets its emoji
        clip["hook"] = "" if (len(h.split()) < 6 and not clip.get("no_hook")) else brain._with_emoji(h, clip.get("category") or "other")
        changes = {}
    new = dict(clip)
    for k in ("start", "end"):
        if k in changes:
            new[k] = round(float(changes[k]), 2)
    if "src_offset" in clip:  # live clip: can only move inside the saved piece of stream
        new["start"] = max(new["start"], clip["src_offset"])
        new["end"] = min(new["end"], clip["src_end"])
    if not (math.isfinite(new["start"]) and math.isfinite(new["end"])) or new["start"] < 0:
        raise ValueError("Those times aren't valid.")
    if not 3 <= new["end"] - new["start"] <= 180:
        raise ValueError("A clip can be 3 to 180 seconds long.")
    for k in ("hook", "title", "description", "style", "layout"):
        if k in changes:
            new[k] = str(changes[k])[:{"hook": 140, "title": 100, "description": 2000}.get(k, 20)]
    if "hook" in changes:           # an emptied hook means "no hook", not "write me one"
        new["no_hook"] = not new["hook"].strip()
    if "cap_lang" in changes:       # captions: Hinglish in English letters (as said), or translated into English
        new["cap_lang"] = "english" if changes["cap_lang"] == "english" else "roman"
        if new["cap_lang"] != clip.get("cap_lang", "roman") and not changes.get("words"):
            new.update(words_edited=False, fixed_for=None)
    if "sfx" in changes:            # meme sounds: [{name, file, t (seconds of the source), db}]
        out = []
        for x in (changes["sfx"] if isinstance(changes["sfx"], list) else [])[:12]:
            try:
                t, db = float(x["t"]), float(x.get("db") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if re.fullmatch(r"[\w\-.]{1,120}\.mp3", str(x.get("file") or "")) and math.isfinite(t) and math.isfinite(db):
                out.append({"name": str(x.get("name") or "")[:80], "file": str(x["file"]), "t": round(t, 2), "db": round(max(-20, min(10, db)), 1)})
        new["sfx"] = sorted(out, key=lambda x: x["t"])
    if "sfx_db" in changes:         # how loud all meme sounds are on this clip
        v = changes["sfx_db"]
        new["sfx_db"] = None if v is None else round(max(-40.0, min(6.0, float(v))), 1)
    if "no_outro" in changes:       # this clip without the outro
        new["no_outro"] = bool(changes["no_outro"])
    if "auto_edit" in changes:      # Auto edit: cut the silences and the ums
        new["auto_edit"] = bool(changes["auto_edit"])
    if "aspect" in changes:
        new["aspect"] = str(changes["aspect"]) if str(changes["aspect"]) in render.ASPECTS else "9:16"
    if "crop_x" in changes:         # where the crop window sits across the picture, 0 to 1
        cx = float(changes["crop_x"])
        new["crop_x"] = round(min(1.0, max(0.0, cx)), 4) if math.isfinite(cx) else 0.5
    if "logo" in changes:
        new["logo"] = bool(changes["logo"])
    if "watermark" in changes:
        new["watermark"] = str(changes["watermark"])[:40]
    for k, lo, hi in (("logo_x", 0.0, 1.0), ("logo_y", 0.0, 1.0), ("sharpen", 0.0, 2.0), ("cap_scale", 0.5, 2.0), ("hook_scale", 0.5, 2.0), ("fscale", 1.0, 3.0), ("crop_y", 0.0, 1.0), ("split_h", 0.3, 0.7), ("band_y", 0.0, 1.0), ("cam_h", 0.2, 0.65), ("band_s", 0.4, 1.6), ("band_x", 0.0, 1.0)):
        if k in changes:            # text sizes; how far the frame is zoomed in and how high it sits
            if changes[k] is None and k == "cam_h":
                new[k] = None       # facecam height back to automatic
                continue
            v = float(changes[k])
            new[k] = round(min(hi, max(lo, v)), 3) if math.isfinite(v) else (0.5 if k in ("logo_x", "crop_y", "split_h", "band_y", "band_x") else 0.35 if k == "cam_h" else 1.0)
    for k in ("font", "hook_font"):  # one of the fonts on this machine, or "" for the style's own
        if k in changes:
            new[k] = str(changes[k]) if str(changes[k]) in render.fonts() else ""
    for k in ("cap_color", "cap_hi", "cap_ol", "hook_ink", "hook_boxc"):     # colours picked by hand, "" = as the style has it
        if k in changes:
            v = str(changes[k] or "")
            new[k] = v.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else ""
    for k, hi in (("cap_stroke", 20), ("cap_shadow", 16)):                  # outline and shadow thickness; None = as the style has it
        if k in changes:
            v = changes[k]
            new[k] = max(0, min(hi, int(v))) if isinstance(v, (int, float)) and math.isfinite(v) else None
    if "cap_box" in changes:
        new["cap_box"] = bool(changes["cap_box"]) if changes["cap_box"] is not None else None
    if "hook_nobox" in changes:
        new["hook_nobox"] = None if changes["hook_nobox"] is None else bool(changes["hook_nobox"])
    if "mask" in changes:           # a cut-out pasted on the clip: what is cut from the picture, and where and how big it sits
        new["mask"] = _clean_mask(changes["mask"])
    if "marks" in changes:          # where the clip was cut with the scissors
        v = changes["marks"] if isinstance(changes["marks"], list) else []
        new["marks"] = sorted({round(float(x), 2) for x in v[:60] if isinstance(x, (int, float)) and math.isfinite(x)})
    if "cuts" in changes:           # parts deleted from inside the clip: [[from, to], ...]
        v = changes["cuts"] if isinstance(changes["cuts"], list) else []
        out = []
        for c in v[:60]:
            try:
                a, b = float(c[0]), float(c[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if math.isfinite(a) and math.isfinite(b) and b - a >= 0.05:
                out.append([round(a, 2), round(b, 2)])
        new["cuts"] = sorted(out)
    if "masks" in changes:          # the mask split into parts of the timeline, each with its own cut-out or none
        v = changes["masks"]
        out = []
        if isinstance(v, list):
            for part in v[:16]:
                try:
                    a, b = float(part["a"]), float(part["b"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(a) and math.isfinite(b) and b - a >= 0.05:
                    out.append({"a": round(a, 2), "b": round(b, 2), "m": _clean_mask(part.get("m"))})
        out.sort(key=lambda p: p["a"])
        new["masks"] = out or None
    if changes.get("words") == "auto":     # captions written again from what is said, for the clip's new length
        new.update(words_edited=False, fixed_words=None, fixed_for=None)
    elif "words" in changes and isinstance(changes["words"], list):    # captions typed by hand: kept exactly as written
        out = []
        for w in changes["words"][:3000]:
            try:
                t, s0, e0 = str(w["w"]).strip()[:40], float(w["s"]), float(w["e"])
            except (KeyError, TypeError, ValueError):
                continue
            if t and math.isfinite(s0) and math.isfinite(e0) and e0 >= s0:
                out.append({"w": t, "s": round(s0, 2), "e": round(e0, 2),
                            **{key: 1 for key in ("gs", "br", "join") if w.get(key)}})
        out.sort(key=lambda w: w["s"])
        new.update(fixed_words=out, words_edited=True)
    if "no_caps" in changes:        # captions switched off
        new["no_caps"] = bool(changes["no_caps"])
    if changes.get("overlay_mode") in ("captions", "hook"):
        new["overlay_mode"] = changes["overlay_mode"]
    for key, limit in (("cold_open", 1), ("bleeps", 120)):
        if key not in changes:
            continue
        items = changes[key]
        if key == "cold_open":
            items = [items] if items else []
        spans = []
        for item in items[:limit] if isinstance(items, list) else []:
            try:
                a, b = map(float, item)
                a, b = max(new.get("start", clip["start"]), a), min(new.get("end", clip["end"]), b)
                if math.isfinite(a) and math.isfinite(b) and b - a >= .02:
                    spans.append([round(a, 3), round(min(a + 12, b) if key == "cold_open" else b, 3)])
            except (TypeError, ValueError, OverflowError):
                continue
        new[key] = (spans[0] if spans else None) if key == "cold_open" else spans
    if "duck_sfx" in changes:
        new["duck_sfx"] = bool(changes["duck_sfx"])
    if isinstance(changes.get("impacts"), list):
        impacts = []
        for item in changes["impacts"][:8]:
            try:
                t, length, strength = (float(item[k]) for k in ("t", "length", "strength"))
                kind = item.get("kind")
                if (kind in ("impact", "bass", "whip") and all(map(math.isfinite, (t, length, strength)))
                        and new["start"] <= t < new["end"] and .12 <= length <= 2 and .1 <= strength <= 1):
                    impacts.append({"t": round(t, 3), "length": length, "strength": strength, "kind": kind})
            except (TypeError, KeyError, AttributeError, ValueError):
                continue
        new["impacts"] = impacts
    if "texts" in changes:          # text added by hand: up to 8 pieces, each with its place, size, colours and font
        out = []
        for t in (changes["texts"] if isinstance(changes["texts"], list) else [])[:24]:
            if not isinstance(t, dict) or not str(t.get("t") or "").strip():
                continue
            num = lambda k, d, lo, hi: min(hi, max(lo, float(t[k]))) if isinstance(t.get(k), (int, float)) and math.isfinite(t[k]) else d   # noqa: E731
            col = lambda k: str(t.get(k)).lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", str(t.get(k) or "")) else ""                       # noqa: E731
            out.append({"t": str(t["t"])[:80], "x": round(num("x", 0.5, 0.02, 0.98), 4), "y": round(num("y", 0.5, 0.02, 0.98), 4),
                        "size": int(num("size", 90, 20, 400)), "stroke": int(num("stroke", 5, 0, 20)), "color": col("color"), "ol": col("ol"),
                        "shadow": bool(t.get("shadow")), "font": str(t.get("font")) if str(t.get("font") or "") in render.fonts() else ""})
            if t.get("sticker"):
                name = str(t["sticker"])
                if not re.fullmatch(r"meme-[a-f0-9]{32}\.png", name) or not (Path(job["dir"]) / "memes" / name).is_file():
                    out.pop()
                    continue
                out[-1].update(sticker=name, size=int(num("size", 700, 100, 1080)))
            if t.get("emoji"):        # an emoji: shown only between its two moments (seconds of the source)
                out[-1].update(emoji=True, t=str(t["t"])[:8], size=int(num("size", 220, 40, 900)))
            if t.get("emoji") or ("a" in t and "b" in t):
                out[-1].update(a=round(num("a", 0, 0, 1e7), 2), b=round(num("b", 1e7, 0, 1e7), 2))
        new["texts"] = out
    if "caps" in changes:           # all capitals, all small, or "" for how the style does it
        new["caps"] = str(changes["caps"]) if str(changes["caps"]) in ("upper", "lower") else ""
    if "hook_span" in changes:
        span = changes.get("hook_span")
        new["hook_span"] = (list(map(float, span)) if isinstance(span, list) and len(span) == 2
                            and all(isinstance(t, (int, float)) and math.isfinite(t) for t in span)
                            and span[1] - span[0] >= .05 else None)
    for k in ("hook_pos", "cap_pos", "watermark_pos"):   # where the text was dragged to, as fractions of the frame; None = where it sits by itself
        if k in changes:
            v = changes[k]
            ok = isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)
            new[k] = [round(min(0.97, max(0.03, float(x))), 4) for x in v] if ok else None
    if "cam_box" in changes:        # the streamer's camera in the picture: x, y, width, height as fractions
        v = changes["cam_box"]
        ok = isinstance(v, (list, tuple)) and len(v) == 4 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)
        new["cam_box"] = [round(min(1.0, max(0.0, float(x))), 4) for x in v] if ok and v[2] >= 0.04 and v[3] >= 0.04 else None
    if "cam_keys" in changes:       # manual facecam tracking keyframes: [{"t": sec, "box": [x, y, w, h]}, ...]
        v = changes["cam_keys"]
        out = []
        if isinstance(v, list):
            for item in v[:60]:
                if not isinstance(item, dict):
                    continue
                try:
                    t = float(item["t"])
                    b = item["box"]
                    ok = isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in b)
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(t) and ok and b[2] >= 0.04 and b[3] >= 0.04:
                    out.append({"t": round(t, 2), "box": [round(min(1.0, max(0.0, float(x))), 4) for x in b]})
        out.sort(key=lambda k: k["t"])
        new["cam_keys"] = out
    if "audio_offset" in changes:   # manual audio-layer nudge in seconds (+ = delay audio relative to video)
        v = float(changes["audio_offset"] or 0.0)
        new["audio_offset"] = round(min(30.0, max(-30.0, v)), 3) if math.isfinite(v) else 0.0
    if "audio_sync_caps" in changes:
        new["audio_sync_caps"] = bool(changes["audio_sync_caps"])
    if "mute_audio" in changes:
        new["mute_audio"] = bool(changes["mute_audio"])
    if "track" in changes:          # the crop follows the face
        new["track"] = bool(changes["track"])
    if "hook_seam" in changes:      # Streamer + game template: headline on the line between face and game
        new["hook_seam"] = bool(changes["hook_seam"])
    if "gameplay" in changes:       # which gameplay runs underneath ("" = any)
        new["gameplay"] = str(changes["gameplay"]) if str(changes["gameplay"]) in render.gameplay_files() else ""
    if "look" in changes:           # effects: a colour look, a zoom move, and four switches
        new["look"] = str(changes["look"]) if str(changes["look"]) in render.LOOKS else "none"
    if "zoom" in changes:
        new["zoom"] = str(changes["zoom"]) if str(changes["zoom"]) in render.ZOOMS else "none"
    for k in render.FX_FLAGS:
        if k in changes:
            new[k] = bool(changes[k])
    if new.get("style") not in render.STYLES:
        new["style"] = DEFAULTS["style"]
    if new.get("layout") not in render.LAYOUTS:
        new["layout"] = DEFAULTS["layout"]
    clip.update(new, status="queued")
    save(job)
    _q.put(("clip", jid, cid))


def more(jid, n=6):
    """Look through a finished video again for moments that were not clipped the first time, and make them.
    What is already a clip is hidden from the AI, and on a long video a different half hour is read on each press,
    so pressing again gives different clips."""
    job = _jobs[jid]
    d = Path(job["dir"])
    src = Path(job.get("source_path") or "")
    if job["status"] != "done" or job.get("kind") == "live":
        raise ValueError("Wait until this video has finished.")
    if not (d / "words.json").is_file() or not src.is_file():
        raise ValueError("The original video is no longer kept, so new clips can't be cut from it. Paste the link again.")
    if not config.ai_ready():
        raise ValueError("Finding more clips needs an AI key. Add one under Settings, AI.")
    if len(job["clips"]) >= 40:
        raise ValueError("This video already has 40 clips.")
    _set(job, status="running", stage="Looking for more moments", progress=0.1, error=None)

    def run():
        import random
        o = job["options"]
        made = 0
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
            taken = [(c["start"] - 2, c["end"] + 2) for c in job["clips"]]
            free = [w for w in words if not any(a <= w["s"] <= b for a, b in taken)]
            total = float(job.get("duration") or (words[-1]["e"] if words else 0))
            span = 30 * 60
            a0 = random.uniform(0, total - span) if total > span * 1.3 else 0.0
            b0 = min(total, a0 + span) if total > span * 1.3 else total
            part = [dict(w, s=round(w["s"] - a0, 2), e=round(w["e"] - a0, 2)) for w in free if a0 <= w["s"] < b0]
            usage.set_context(job.get("creator"), job.get("title") or "")
            found, ai_ = brain.find_clips(part, [], b0 - a0, job.get("title") or "", job.get("uploader") or "", int(n),
                                          int(o["min_len"]), int(o["max_len"]), lambda m: None,
                                          focus=job.get("focus", ""), lang=job.get("audio_lang") or job.get("lang") or "") if len(part) >= 8 else ([], {})
            found = [c for c in found if not any(a < c["end"] + a0 and c["start"] + a0 < b for a, b in taken)]
            num = max([int(c["id"][4:]) for c in job["clips"] if c["id"][4:].isdigit()] or [0])
            new = []
            for c in found:
                num += 1
                c.update(id=f"clip{num:02d}", start=round(c["start"] + a0, 2), end=round(c["end"] + a0, 2),
                         style=o["style"], layout=o["layout"], cap_lang=o.get("cap_lang", "roman"), status="queued", version=0)
                if o.get("aspect"):
                    c["aspect"] = o["aspect"]
                _attach_clip_meta(job, c, words_hint=[w for w in words if c["start"] - 0.5 <= w["s"] <= c["end"] + 0.5],
                                  whisper_lang=job.get("lang") or "")
                job["clips"].append(c)
                new.append(c)
            _set(job, stage=f"Making {len(new)} more clip{'s' if len(new) != 1 else ''}" if new else "Looking for more moments", progress=0.4)
            for i, c in enumerate(new):
                if job["source"]["type"] == "url" and not media.has_1080p(job):
                    _asked_piece(job, c)
                else:
                    render_one(job, c, words)
                made += c.get("status") == "done"
                _set(job, progress=0.4 + 0.55 * (i + 1) / len(new))
            job["ai"] = dict(job.get("ai") or {}, tokens=int((job.get("ai") or {}).get("tokens", 0)) + int(ai_.get("tokens", 0)),
                             note="" if new else "No new moments worth clipping were found in that part. Press again to look at another part.")
        except Cancelled:
            job["clips"] = [c for c in job["clips"] if c.get("status") == "done"]
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            job["clips"] = [c for c in job["clips"] if c.get("status") in ("done", "error")]
            job["ai"] = dict(job.get("ai") or {}, note=f"Could not look for more clips: {safe.friendly(e)[:160]}")
        with _lock:
            job.update(cancel=False, status="done", stage="Done", progress=1.0)
        save(job)

    submit(run)


def split_clip(jid, cid, at: float):
    """Cuts one clip into two at `at` (seconds in the video). Both halves are made again."""
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    at = round(float(at), 2)
    if not (clip["start"] + 3 <= at <= clip["end"] - 3):
        raise ValueError("Each half has to be at least 3 seconds long.")
    n = max([int(re.sub(r"\D", "", c["id"]) or 0) for c in job["clips"]] + [0]) + 1
    second = {k: v for k, v in clip.items() if k not in ("file", "cover", "fixed_words", "fixed_for", "version", "made_at", "posts", "auto_cut", "perf", "score_status")}
    second.update(id=f"clip{n:02d}", start=at, status="queued", title=(clip.get("title") or "Clip") + " (part 2)")
    clip.update(end=at, status="queued")
    job["clips"].insert(job["clips"].index(clip) + 1, second)
    save(job)
    _q.put(("clip", jid, cid))
    _q.put(("clip", jid, second["id"]))
    return second["id"]


def add_asked(jid, found: list, ask: str) -> list:
    """Clips the person asked for in words (askclip.find): added to the video and made like any other clip."""
    job = _jobs[jid]
    o = job["options"]
    made = []
    for c in found:
        n = max([int(re.sub(r"\D", "", k["id"]) or 0) for k in job["clips"]] + [0]) + 1
        clip = {"id": f"clip{n:02d}", "start": c["start"], "end": min(c["end"], job.get("duration") or c["end"]),
                "title": c.get("title") or ask[:80], "hook": c.get("hook") or "", "score": 0, "category": "asked",
                "reason": f"You asked: {ask[:120]}" + (f" ({c['why']})" if c.get("why") else ""), "style": o.get("style"),
                "layout": o.get("layout"), "status": "queued", "version": 0, "asked": True, "description": "", "hashtags": [],
                "highlight_words": []}
        job["clips"].append(clip)
        made.append(clip)
    save(job)
    for clip in made:
        src = job.get("source_path")
        if src and Path(src).is_file() and (job["source"]["type"] != "url" or media.has_1080p(job)):
            _q.put(("clip", jid, clip["id"]))
        else:
            submit(lambda clip=clip: _asked_piece(job, clip))
    return [c["id"] for c in made]


def _asked_piece(job, clip):
    """A long video kept only its clips' pieces: fetch this new clip's stretch, then make it."""
    d = Path(job["dir"])
    full_audio = None
    try:
        wav = d / "audio.wav"
        if not wav.is_file() and Path(job.get("source_path") or "").is_file():
            wav = d / "clips" / f"{clip['id']}.align.wav"
            media.extract_audio(Path(job["source_path"]), wav)
        try:
            full_audio = media.load_audio(wav, mapped=True) if wav.is_file() else None
            _fetch_piece(job, clip, full_audio)
            if clip["status"] == "error":
                save(job)
                return
        finally:
            if wav.parent == d / "clips":
                del full_audio
                wav.unlink(missing_ok=True)
        words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        render_one(job, clip, words)
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error="Couldn't fetch that part of the video: " + str(e)[-120:])
        save(job)


GENERIC_HOOKS = {"", "WAIT FOR IT"}


def auto_fill(job, clip, words, src, off):
    """Before a clip is made: a real hook and title if it has none, and the crop framed on the speaker's face."""
    if clip.get("no_hook"):          # the hook was removed by hand: it stays removed
        clip["hook"] = ""
    elif (clip.get("hook") or "").strip().upper() in GENERIC_HOOKS or not clip.get("title") or str(clip.get("title")).startswith("Loud moment"):
        try:
            text = " ".join(str(w.get("w", "")) for w in words if clip["start"] - 0.2 <= w["s"] <= clip["end"])
            usage.set_context(job.get("creator"), job.get("title") or "")
            cap, _ = brain.write_caption(text, job.get("title") or "", job.get("uploader") or "")
            part = " (part 2)" if str(clip.get("title") or "").endswith("(part 2)") else ""
            if cap.get("hook"):
                clip["hook"] = cap["hook"][:60]
            if cap.get("title"):
                clip["title"] = (cap["title"][:90] + part)
            for k in ("description", "hashtags", "highlight_words"):
                if cap.get(k):
                    clip[k] = cap[k]
        except Exception:  # noqa: BLE001 - the clip is still made
            traceback.print_exc()
    if "crop_x" not in clip and clip.get("layout") not in ("facegame", "cam"):
        try:
            from . import faces
            box = faces.find(src, clip["start"] - off, clip["end"] - off)
            if box:
                aw, ah = render.ASPECTS.get(clip.get("aspect"), render.ASPECTS["9:16"])
                wf = min(1.0, (job["height"] * aw / ah) / max(1, job["width"]))
                clip["face"] = box
                if wf < 1:
                    clip["crop_x"] = round(min(1.0, max(0.0, (box["x"] + box["w"] / 2 - wf / 2) / (1 - wf))), 4)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        clip.setdefault("crop_x", 0.5)


def follow(clip, src, off=0.0):
    """When the clip is set to follow the face, work out where the face goes (once for each start and end)."""
    if not clip.get("track") or clip.get("layout") in ("facegame", "cam"):
        return
    span = [round(clip["start"], 2), round(clip["end"], 2)]
    if clip.get("track_for") != span:
        try:
            from . import faces
            clip["track_keys"] = faces.track(src, clip["start"] - off, clip["end"] - off)
        except Exception:  # noqa: BLE001 - the clip is still made, framed where it was
            traceback.print_exc()
            clip["track_keys"] = []
        clip["track_for"] = span


def render_one(job, clip, words):
    d = Path(job["dir"])
    clip["status"] = "rendering"
    save(job)
    try:
        off = clip.get("src_offset", 0.0)
        src = Path(clip["src"]) if clip.get("src") else Path(job["source_path"])
        if not clip.get("src"):
            clip["end"] = min(clip["end"], job["duration"])
        source_meta = media.probe(src)
        if not media.has_1080p(source_meta):
            raise ValueError("1080p source unavailable. No clip was rendered.")
        fast = bool(job.get("options", {}).get("fast_render"))
        if not fast:
            auto_fill(job, clip, words, src, off)
        follow(clip, src, off)
        if clip.get("layout") in ("facegame", "cam") and not clip.get("cam_box"):
            cr = creators.get(job["creator"]) if job.get("creator") else None
            if cr and cr.get("cam_box"):
                clip["cam_box"] = cr["cam_box"]
                if cr.get("cam_h") and not clip.get("cam_h"):
                    clip["cam_h"] = cr["cam_h"]
        hook_txt = clip.get("hook") or ""
        hook_txt = roman.to_roman(hook_txt) if roman.should_romanize_text(hook_txt, job.get("lang") or "") else hook_txt
        local = dict(clip, start=clip["start"] - off, end=clip["end"] - off, hook=hook_txt)
        local["auto_cut_enabled"] = job.get("options", {}).get("auto_cut", False)
        a_idx = clip.get("audio_stream_index") if clip.get("audio_stream_index") is not None else job.get("audio_stream_index")
        if a_idx is not None:
            local["audio_stream_index"] = a_idx
        if any(t.get("sticker") for t in local.get("texts") or []):
            local["meme_dir"] = str(Path(job["dir"]) / "memes")
        selected_lang = (job.get("options") or {}).get("lang") or "auto"
        selected_lang = "" if selected_lang == "auto" else selected_lang.lower()
        audio_lang = job.get("audio_lang") or job.get("lang") or ""
        eng = (clip.get("cap_lang") or (job.get("options") or {}).get("cap_lang") or "roman") == "english"
        need_lang_pass = bool(selected_lang and selected_lang != "en") or eng
        span = [round(clip["start"], 2), round(clip["end"], 2), "tr4" if eng else "src4", selected_lang, audio_lang]
        hl = ""
        lang = selected_lang or audio_lang
        if fast and not need_lang_pass and not clip.get("words_edited"):
            raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        elif clip.get("words_edited") and isinstance(clip.get("fixed_words"), list):
            raw = clip["fixed_words"]          # typed by hand in the editor: never rewritten
        elif clip.get("fixed_for") == span and clip.get("fixed_words") and not roman.should_romanize(
                clip["fixed_words"], selected_lang or audio_lang):
            raw = clip["fixed_words"]          # already tidied for this exact span: no second AI call
        else:
            raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
            from . import hear
            if hear.ready():               # Whisper hears this clip again: right words in source language
                try:
                    hw, hl = hear.clip_words(src, clip["start"] - off - 1, clip["end"] - off + 1, off,
                                             job.get("title") or "", lang=selected_lang,
                                             expected_lang=selected_lang or audio_lang,
                                             stream_index=a_idx)
                    hw_in = [w for w in hw if clip["start"] - 0.15 <= w["s"] <= clip["end"] + 0.15]
                    raw_in = [w for w in raw if clip["start"] - 0.15 <= w["s"] <= clip["end"] + 0.15]
                    min_hw = max(3, int(len(raw_in) * 0.30)) if len(raw_in) >= 12 else 3
                    if len(hw_in) >= min_hw:
                        det_base = media.base_lang(hl)
                        raw = hear.streamer_only(hear.tidy_mixed(hw, lang=selected_lang or det_base),
                                                 job.get("uploader") or "", title=job.get("title") or "")
                        if not selected_lang and det_base:
                            lang = det_base
                        clip["heard_by"] = "whisper"
                except Exception:  # noqa: BLE001 - the built-in words are used
                    pass
            if clip.get("heard_by") != "whisper" and config.ai_ready():     # without Whisper too: no game dialogue
                try:
                    raw = hear.streamer_only(raw, job.get("uploader") or "", title=job.get("title") or "")
                except Exception:  # noqa: BLE001
                    pass
            try:
                usage.set_context(job.get("creator"), job.get("title") or "")
                if eng:
                    raw = brain.translate_caption_words(raw, lang or "hi", job.get("title") or "")
                    if roman.should_romanize(raw, "hi"):
                        raw = roman.words(raw)
                elif clip.get("heard_by") == "whisper" and not roman.should_romanize(raw, lang):
                    pass  # Whisper already transcribed clean verbatim captions in the spoken language
                else:
                    raw = brain.english_captions(raw, lang, job.get("title") or "",
                                                 youtube_english=None, translate=False)
                clip.update(fixed_words=raw, fixed_for=span)
            except Exception:  # noqa: BLE001 - captions still render with the words as heard
                pass
        if roman.should_romanize(raw, lang):
            raw = roman.words(raw)
        near = [dict(w, s=w["s"] - off, e=w["e"] - off) for w in raw]
        if local["auto_cut_enabled"]:
            local["auto_cut_words"] = near + [dict(w, s=w["s"] - off, e=w["e"] - off) for w in words
                                              if w["e"] > clip["start"] and w["s"] < clip["end"]]
        _attach_clip_meta(job, clip, words_hint=raw, whisper_lang=hl or lang)
        if clip.get("sfx") is None:      # meme sounds are never added by themselves: only by hand or "Let the AI add"
            clip["sfx"] = []
            local["sfx"] = []
        if off:     # the clip is cut from a piece of the video: every saved moment moves onto the piece's clock
            sh = lambda v: round(float(v) - off, 3)        # noqa: E731
            if local.get("masks"):
                local["masks"] = [dict(m, a=sh(m["a"]), b=sh(m["b"])) for m in local["masks"]]
            if local.get("cam_keys"):
                local["cam_keys"] = [dict(k, t=sh(k["t"])) for k in local["cam_keys"]]
            if local.get("cuts"):
                local["cuts"] = [[sh(a_), sh(b_)] for a_, b_ in local["cuts"]]
            if local.get("hook_span"):
                local["hook_span"] = [sh(v) for v in local["hook_span"]]
            if local.get("texts"):
                local["texts"] = [dict(t, a=sh(t["a"]), b=sh(t["b"])) if "a" in t and "b" in t else t for t in local["texts"]]
            if local.get("sfx"):
                local["sfx"] = [dict(x, t=sh(x["t"])) for x in local["sfx"]]
            if local.get("impacts"):
                local["impacts"] = [dict(x, t=sh(x["t"])) for x in local["impacts"]]
            if local.get("cold_open"):
                local["cold_open"] = [sh(t) for t in local["cold_open"]]
            if local.get("bleeps"):
                local["bleeps"] = [[sh(a), sh(b)] for a, b in local["bleeps"]]
        of, oon = config.outro()
        local["outro"] = str(of) if of and oon and not clip.get("no_outro") and job.get("kind") != "live" else None
        clip["stage"] = "Rendering the 1080p clip"
        save(job)
        out = render.render_clip(src, d / "clips", clip["id"], local, near, source_meta["width"], source_meta["height"])
        clip.pop("stage", None)
        clip.pop("perf", None)
        clip.pop("score_status", None)
        clip.update(out, status="done", version=clip.get("version", 0) + 1, error=None)
        post = {k: clip.get(k, [] if k == "hashtags" else "") for k in (
            "title", "description", "hashtags", "hook", "start", "end", "score",
            "source_timestamps", "duration", "export_duration", "detected_language", "detected_lang_code", "reason", "audio_track")}
        (d / "clips" / f"{clip['id']}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
        if clip["version"] == 1:
            clip.setdefault("made_at", time.time())
            creators.log(job["creator"], f"Clip ready (score {clip['score']}): {clip['title'] or clip['hook']}")
            save(job)
            for fn in on_clip_ready:
                try:
                    fn(job, clip)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=safe.friendly(e, 400))
        clip.pop("stage", None)
        creators.log(job["creator"], f"Clip failed: {safe.friendly(e, 200)}")
    save(job)
    if clip.get("status") == "done":
        from . import scores
        scores.queue(job, clip)


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


# above this, fetch only the pieces that become clips (YouTube throttles ranged reads, so keep it high)
LONG_VIDEO = float(os.environ.get("PEAKCUT_PIECES_ABOVE_MIN", "60")) * 60
PAD = 20               # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _fetch_piece(job, clip, full_audio):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    wav = d / "clips" / f"{clip['id']}.wav"
    try:
        padding = 8 if job["options"].get("quick_replay") else PAD
        want = max(0.0, clip["start"] - padding)
        till = clip["end"] + padding
        if job.get("duration"):
            till = min(job["duration"], till)
        url, stem = job["source"]["url"], f"{clip['id']}.hd.src"
        req_track = (job.get("options") or {}).get("audio_track") or ""
        existing = d / "clips" / (stem + ".mp4")
        try:
            reusable = existing.is_file() and media.has_1080p(media.probe(existing))
        except RuntimeError:
            reusable = False
        if reusable:
            piece = existing
        elif job["source"].get("past") and media.is_youtube(url):
            from . import past
            origin = float(job["source"].get("past_from") or 0)
            piece = past.youtube_section(url, d / "clips", stem, want + origin, till + origin,
                                         1080, require_hd=True, audio_track=req_track)
        else:
            piece = media.download_section(url, d / "clips", stem, want, till, 1080,
                                           require_hd=True, audio_track=req_track)
        meta = media.probe(piece)
        if not media.has_1080p(meta):
            raise ValueError("The platform returned a lower-resolution source.")
        off = want
        if full_audio is not None:
            media.extract_audio(piece, wav, stream_index=meta.get("audio_stream_index"))
            off = media.find_offset(full_audio, media.load_audio(wav), want)
        if off > clip["start"] + .25 or off + meta["duration"] < clip["end"] - .25:
            raise ValueError("The 1080p source did not cover the selected moment.")
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2),
                    audio_stream_index=meta.get("audio_stream_index"))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Exception as e:  # keep the selected candidate visible with the reason it failed
        clip.update(status="error", error=f"1080p source unavailable. {safe.friendly(e, 200)}")
    finally:
        wav.unlink(missing_ok=True)


BLOCK = 10 * 60         # long videos are transcribed and clipped one block at a time
PROGRESSIVE_FROM = 20 * 60


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _moving_picture(clip):
    """A conservative activity check. It screens static slides, not the identity of the game."""
    import numpy as np
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="upfern-motion-") as temp:
        raw = Path(temp) / "frames.bin"
        start = max(0, clip["start"] - clip["src_offset"])
        duration = clip["end"] - clip["start"]
        media.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", clip["src"],
                   "-t", str(duration), "-vf", "crop=iw*0.70:ih*0.68,fps=1,scale=96:54,format=gray",
                   "-an", "-f", "rawvideo", str(raw)], timeout=25)
        data = np.fromfile(raw, dtype=np.uint8)
        if len(data) < 3 * 96 * 54:
            return False
        frames = data.reshape(-1, 54, 96).astype(np.int16)
        changed = (np.abs(np.diff(frames, axis=0)) >= 17).mean(axis=(1, 2))
        return float((changed >= .055).mean()) >= .28


def _quick_replay(job, resuming, t0):
    """Scan short windows and render one verified HD result at a time."""
    from tempfile import TemporaryDirectory
    from . import hear as hear_mod

    d = Path(job["dir"])
    source, o = job["source"], job["options"]
    url = source["url"]
    req_track = str(o.get("audio_track") or "")
    meta = media.info(url)
    if meta["kind"] != "video" or meta.get("is_live"):
        raise ValueError("Paste a finished YouTube or Twitch replay, or record a live channel under Creators.")
    low = max(0.0, o.get("range_start") or 0)
    high = min(meta["duration"], o.get("range_end") or meta["duration"])
    if high - low < o["min_len"]:
        raise ValueError("The selected range is shorter than the minimum clip length.")
    sel = media.select_audio_track(meta, req_track) if media.is_youtube(url) else {
        "id": "default", "lang": meta.get("language") or "", "label": "Original",
        "verified": True, "is_dubbed": False, "explicit": False,
    }
    try:
        tracks = media.replay_tracks(url, audio_track=req_track)
    except (media.YouTubeBlocked, media.UnverifiedAudioTrackError):
        raise
    except Exception:
        return False
    if not tracks:
        return False
    selected_lang = "" if o.get("lang", "auto") == "auto" else str(o["lang"]).lower()
    _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"],
         audio_track={"id": sel["id"], "lang": sel["lang"], "label": sel["label"],
                      "verified": sel["verified"], "is_dubbed": sel["is_dubbed"], "explicit": sel["explicit"]},
         audio_lang=sel["lang"], audio_is_dubbed=sel["is_dubbed"],
         lang=selected_lang or sel["lang"] or "")
    o["fast_render"] = True
    yt_quick = None
    if config.YT_TRANSCRIPT and media.is_youtube(url) and not sel["is_dubbed"]:
        try:
            yt_quick = media.youtube_words(url, lang=selected_lang, audio_lang=sel["lang"], is_dubbed=sel["is_dubbed"])
        except Exception:  # noqa: BLE001
            yt_quick = None
    if yt_quick and not selected_lang and yt_quick[1]:
        job["lang"] = yt_quick[1]
    words_file = d / "words.json"
    words = json.loads(words_file.read_text(encoding="utf-8")) if resuming else []
    for clip in job["clips"]:
        if clip["status"] in ("queued", "rendering"):
            _fetch_piece(job, clip, None)
            if clip["status"] != "error":
                render_one(job, clip, words)
    want = o["max_clips"]
    windows = min(24, max(2, want * 2))
    # Start a little into long recordings to avoid the typical standby screen,
    # then cover the requested range in evenly spaced windows.
    length = max(90, o["max_len"] * 2)
    start = min(low + (75 if high - low > 480 and not low else 0), max(low, high - length))
    step = max(length, (high - start - length) / max(1, windows - 1))
    windows = [(a, min(a + length, high)) for n in range(windows)
               if (a := start + step * n) + o["min_len"] <= high]
    begin = int(job.get("quick_windows", 0))
    scanned = bool(begin)
    eff_track_lang = selected_lang or sel["lang"] or ""
    use_whisper = bool(((eff_track_lang and eff_track_lang != "en") or sel["is_dubbed"]) and hear_mod.ready())
    for index, (a, b) in enumerate(windows):
        if index < begin:
            continue
        if job.get("cancel"):
            raise Cancelled()
        if sum(c["status"] == "done" for c in job["clips"]) >= want:
            break
        _set(job, stage=f"Scanning replay near {int(a / 60)} min", progress=min(.94, .05 + .85 * index / len(windows)))
        with TemporaryDirectory(prefix="quick-", dir=d) as temp:
            wav = Path(temp) / "scan.wav"
            try:
                media.replay_audio_window(tracks, a, b, wav)
            except Exception:
                if not scanned and not job["clips"] and index >= 1:
                    return False
                continue
            audio = media.load_audio(wav)
            if len(audio) < .85 * (b - a) * 16000:
                continue
            scanned = True
            if yt_quick and _covers(yt_quick[0], a, b):
                window_words = [dict(w, s=round(w["s"] - a, 2), e=round(w["e"] - a, 2))
                                for w in yt_quick[0] if a <= w["s"] < b]
            elif use_whisper:
                window_words, det_q = hear_mod.transcribe_audio_chunks(audio, lang=eff_track_lang, title=job.get("title") or "")
                if det_q and not selected_lang:
                    job["lang"] = det_q
            else:
                window_words = transcribe.transcribe(audio)
            fresh = [dict(w, s=w["s"] + a, e=w["e"] + a) for w in window_words]
            words = sorted([w for w in words if not a <= w["s"] < b] + fresh, key=lambda w: w["s"])
            tmp = words_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(words), encoding="utf-8")
            tmp.replace(words_file)
            afeats = media.audio_features_per_second(audio)
            if config.ai_ready():
                candidates, ai_q = brain.find_clips(
                    window_words, afeats["db"], b - a, job.get("title") or "", job.get("uploader") or "",
                    min(4, max(2, want - sum(c["status"] == "done" for c in job["clips"]))),
                    int(o["min_len"]), int(o["max_len"]), lambda m: None,
                    lean=False, focus=job.get("focus", ""), spike=afeats["spike"],
                    lang=job.get("lang") or eff_track_lang, quality_threshold=45.0,
                )
                if ai_q.get("model"):
                    cur_ai = job.get("ai") or {}
                    job["ai"] = {"model": ai_q["model"], "tokens": int(cur_ai.get("tokens", 0)) + int(ai_q.get("tokens", 0)), "note": ""}
            else:
                candidates = brain.fallback_by_loudness(afeats["db"], window_words, 3, o["max_len"], b - a,
                                                        spike=afeats["spike"], min_len=int(o["min_len"]))
            for candidate in candidates:
                if job.get("cancel"):
                    raise Cancelled()
                lo, hi = round(candidate["start"] + a, 2), round(candidate["end"] + a, 2)
                if hi - lo < o["min_len"] or any(lo < c["end"] and c["start"] < hi for c in job["clips"]):
                    continue
                n = max((int(re.sub(r"\D", "", c["id"]) or 0) for c in job["clips"]), default=0) + 1
                candidate.update(id=f"clip{n:02d}", start=lo, end=hi, style=o["style"], layout=o["layout"],
                                 aspect=o.get("aspect") or "9:16", cap_lang=o.get("cap_lang", "roman"),
                                 status="rendering", stage="Fetching the 1080p source", version=0)
                _attach_clip_meta(job, candidate, words_hint=[w for w in words if lo - 0.5 <= w["s"] <= hi + 0.5],
                                  whisper_lang=job.get("lang") or "")
                job["clips"].append(candidate)
                save(job)
                _fetch_piece(job, candidate, None)
                candidate.pop("stage", None)
                if candidate["status"] != "error" and o.get("gameplay_check"):
                    try:
                        moving = _moving_picture(candidate)
                    except Exception:
                        moving = False
                    if not moving:
                        job["clips"].remove(candidate)
                        Path(candidate["src"]).unlink(missing_ok=True)
                        save(job)
                        continue
                if candidate["status"] != "error":
                    render_one(job, candidate, words)
                    if candidate["status"] == "done" and not job.get("first_reel_seconds"):
                        job["first_reel_seconds"] = round(time.time() - t0, 1)
                if sum(c["status"] == "done" for c in job["clips"]) >= want:
                    break
        job["quick_windows"] = index + 1
        save(job)
    if not scanned and not job["clips"]:
        return False
    count = sum(c["status"] == "done" for c in job["clips"])
    _set(job, status="done", stage=f"{count} of {want} Reels ready" if count else "No eligible 1080p moments found",
         progress=1, took=round(time.time() - t0))
    return True


def _run_job(job):
    import concurrent.futures
    import math
    from concurrent.futures import ThreadPoolExecutor
    from . import hear as hear_mod

    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    t0 = time.time()
    notes = []
    req_track = str(o.get("audio_track") or "")
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    resuming = bool(job.pop("resume", False)) and (d / "words.json").is_file()
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the audio", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    if (o.get("quick_replay") and src["type"] == "url" and not src.get("past")
            and (media.is_youtube(src["url"]) or media.is_twitch(src["url"]))):
        if _quick_replay(job, resuming, t0):
            return
        _set(job, stage="This replay needs a full scan")
    fetch = ThreadPoolExecutor(1)
    video = None
    wav = d / "audio.wav"
    local_stream_index = job.get("audio_stream_index")
    cached_audio = resuming and wav.is_file() and wav.stat().st_size >= int(max(1, job.get("duration") or 1) * 16000 * 2 * .98)
    if cached_audio and src["type"] == "url":
        # The transcript is already backed by this fully downloaded waveform.
        # Resume pending renders without re-downloading several hours of audio.
        pieces = True
        audio_file = None
    elif src["type"] == "url" and src.get("past") and media.is_youtube(src["url"]):
        # the earlier part of a stream that is live right now: audio of the pieces up to when recording started
        from . import past
        meta = media.info(src["url"])
        sel = media.select_audio_track(meta, req_track)
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=float(src["past"]),
             audio_track={"id": sel["id"], "lang": sel["lang"], "label": sel["label"],
                          "verified": sel["verified"], "is_dubbed": sel["is_dubbed"], "explicit": sel["explicit"]},
             audio_lang=sel["lang"], audio_is_dubbed=sel["is_dubbed"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        pieces = True
        _set(job, stage=f"Getting the {round(src['past'] / 60)} minutes before recording")
        audio_file = past.youtube_audio(src["url"], float(src["past"]), d, lambda p: _set(job, progress=0.03 + 0.1 * p),
                                        start=float(src.get("past_from") or 0), audio_track=req_track)
    elif src["type"] == "url":
        meta = media.info(src["url"])
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part of it under Options.")
        if meta["is_live"] and not src.get("past"):
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        if media.is_youtube(src["url"]):
            sel = media.select_audio_track(meta, req_track)
        else:
            sel = {"id": "default", "lang": meta.get("language") or "", "label": "Original",
                   "verified": True, "is_dubbed": False, "explicit": False}
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"],
             audio_track={"id": sel["id"], "lang": sel["lang"], "label": sel["label"],
                          "verified": sel["verified"], "is_dubbed": sel["is_dubbed"], "explicit": sel["explicit"]},
             audio_lang=sel["lang"], audio_is_dubbed=sel["is_dubbed"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        pieces = span > LONG_VIDEO or meta["duration"] <= 0
        if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
            video = fetch.submit(media.download, src["url"], d, min(720, int(o["quality"])), audio_track=req_track)
        audio_file = media.download_audio(src["url"], d, lambda p: _set(job, progress=0.03 + 0.1 * p),
                                          audio_track=req_track)
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        local_stream_index = info.get("audio_stream_index")
        picked_t = next((t for t in info.get("audio_tracks") or [] if t["audio_index"] == local_stream_index), None)
        t_lang = info.get("audio_lang") or ""
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"],
             audio_stream_index=local_stream_index, audio_lang=t_lang,
             audio_is_dubbed=bool(picked_t and picked_t.get("is_dubbed")),
             audio_track={"id": f"{t_lang or 'default'}:local", "lang": t_lang,
                          "label": picked_t["label"] if picked_t else "Original", "verified": True,
                          "is_dubbed": bool(picked_t and picked_t.get("is_dubbed")), "explicit": False})
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    if audio_file is not None:
        media.extract_audio(audio_file, wav, stream_index=local_stream_index)
        if src["type"] == "url":
            audio_file.unlink(missing_ok=True)
    audio = media.load_audio(wav, mapped=True)      # read from the file as needed; deleted when the job is done
    if not job["duration"]:
        job["duration"] = len(audio) / 16000
    lo = int((o["range_start"] or 0) * 16000)
    hi = min(len(audio), int(o["range_end"] * 16000)) if o["range_end"] else len(audio)
    span = (hi - lo) / 16000
    afeats = media.audio_features_per_second(audio[lo:hi])
    db, spike = afeats["db"], afeats["spike"]

    vlock, vstate = threading.Lock(), {}

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        path = media.download(src["url"], d, min(720, int(o["quality"])), audio_track=req_track)
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    selected_lang = "" if o.get("lang", "auto") == "auto" else str(o["lang"]).lower()
    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip

    def make(clip, words_now):
        if job.get("cancel") or job.get("cancelled"):
            return
        if src["type"] == "url":
            full = None if pieces else whole_video()
            if not full or not media.has_1080p(job):
                clip.update(status="rendering", stage="Fetching the 1080p source")
                save(job)
                _fetch_piece(job, clip, audio)
                clip.pop("stage", None)
        eff_lang = selected_lang or job.get("audio_lang") or (yt[1] if yt else "") or job.get("lang") or ""
        if (clip["status"] != "error" and yt and eff_lang.lower().startswith("en")
                and not job.get("audio_is_dubbed") and not hear_mod.ready()):
            # Fallback when Groq Whisper is unavailable: refine English YouTube caption word timings locally.
            a0, b0 = max(lo, int((clip["start"] - 1.5) * 16000)), min(hi, int((clip["end"] + 1.5) * 16000))
            try:
                tw = [dict(w, s=round(w["s"] + a0 / 16000, 2), e=round(w["e"] + a0 / 16000, 2)) for w in transcribe.transcribe(audio[a0:b0])]
            except Exception:  # noqa: BLE001 - the clip is still made, with YouTube's words
                tw = []
            if len(tw) >= 3:
                exact[clip["id"]] = (a0 / 16000, b0 / 16000, tw)
                words_now = tw
        if clip["status"] != "error":
            render_one(job, clip, words_now)

    renders = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-render")
    futures = []
    LONG_DISCOVERY_SEC = 45 * 60
    step = BLOCK * 16000 if span > LONG_DISCOVERY_SEC else hi - lo
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if (not resuming or not job.get("done_blocks")) and src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]) and not src.get("past"):
        try:
            yt = media.youtube_words(src["url"], lang=selected_lang,
                                     audio_lang=job.get("audio_lang") or "",
                                     is_dubbed=bool(job.get("audio_is_dubbed")))
        except Exception:  # noqa: BLE001 - no transcript to borrow: transcribe selected audio directly
            yt = None
        if yt and not _covers(yt[0], lo / 16000, hi / 16000):
            yt = None
    eff_audio_lang = selected_lang or job.get("audio_lang") or ""
    use_whisper_chunks = bool(not yt and ((eff_audio_lang and eff_audio_lang != "en") or job.get("audio_is_dubbed")) and hear_mod.ready())
    if not resuming or not job.get("done_blocks"):
        job["transcript_from"] = "youtube" if yt else ("whisper" if use_whisper_chunks else "own")
        job["lang"] = selected_lang or (yt[1] if yt else "") or job.get("audio_lang") or ""
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except ValueError:
            words = []
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if words else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append(renders.submit(ctx.bind(make), c, list(words)))
        save(job)
    # The transcript of the next part is written while the AI is still reading this one, so neither waits for the other.
    ahead = ThreadPoolExecutor(2, thread_name_prefix="ahead")
    want = int(o["max_clips"])
    if span > 3600 and want == 18:
        want = min(120, max(18, round(span / 3600 * 14))) if o.get("mass") else want
    elif span > 3600 and o.get("mass"):
        want = min(120, max(want, round(span / 3600 * 14)))

    def take(c):
        next_id = max((int(re.sub(r"\D", "", item["id"]) or 0) for item in job["clips"]), default=0) + 1
        c.update(id=f"clip{next_id:02d}", style=o["style"], layout=o["layout"], cap_lang=o.get("cap_lang", "roman"), status="queued", version=0)
        if o.get("aspect"):
            c["aspect"] = o["aspect"]
        _attach_clip_meta(job, c, words_hint=[w for w in words if c["start"] - 0.5 <= w["s"] <= c["end"] + 0.5],
                          whisper_lang=job.get("lang") or "")
        job["clips"].append(c)
        futures.append(renders.submit(ctx.bind(make), c, list(words)))

    heard, now_on = {}, [done_blocks]

    def _schedule_asr(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    _set(job, progress=max(job["progress"], 0.14 + 0.68 * ((a_ - lo) + p * (b_ - a_)) / max(1, hi - lo)))

            if use_whisper_chunks:
                heard[i] = ahead.submit(
                    ctx.bind(hear_mod.transcribe_audio_chunks), audio[a_:b_], eff_audio_lang, job.get("title") or "", tp
                )
            else:
                heard[i] = ahead.submit(ctx.bind(transcribe.transcribe), audio[a_:b_], tp)

    for bi, (a, b) in enumerate(blocks):
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        shift = a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        if yt:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(w["e"] - shift, 2)) for w in yt[0] if a / 16000 <= w["s"] < b / 16000]
        else:
            _set(job, stage="Transcribing selected audio" + where if use_whisper_chunks else "Writing the transcript" + where)
            now_on[0] = bi
            _schedule_asr(bi)
            _schedule_asr(bi + 1)
            res = heard.pop(bi).result()
            if isinstance(res, tuple):
                bw, det_lang = res
                if det_lang and not selected_lang:
                    job["lang"] = det_lang
            else:
                bw = res
        _set(job, progress=max(job["progress"], 0.14 + 0.64 * (b - lo) / max(1, hi - lo)))
        words += [dict(w, s=round(w["s"] + shift, 2), e=round(w["e"] + shift, 2)) for w in bw]
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
        _set(job, stage="Discovering best moments across full video" + where, word_count=len(words))
        if len(bw) < 8:
            job["done_blocks"] = bi + 1
            save(job)
            continue

        s0 = int((a - lo) / 16000)
        s1 = s0 + int((b - a) / 16000) + 1
        visual = None
        if len(blocks) == 1:
            vpath = Path(src["path"]) if src["type"] != "url" else None
            if vpath is None and video is not None:
                concurrent.futures.wait([video], timeout=12)
                if video.done():
                    vpath = whole_video()
            if vpath and Path(vpath).is_file():
                vis_full = media.visual_activity_per_second(Path(vpath), max_seconds=hi / 16000 + 5)
                if len(vis_full) > 5:
                    target_n = len(db[s0:s1])
                    visual = vis_full[int(lo / 16000): int(hi / 16000) + 1]
                    if target_n and len(visual) >= 5:
                        if len(visual) > target_n:
                            visual = visual[:target_n]
                        elif len(visual) < target_n and target_n - len(visual) <= 3:
                            import numpy as np
                            visual = np.pad(visual, (0, target_n - len(visual)), mode="edge")
                    else:
                        visual = None

        cand_target = max(want * 2, 32) if len(blocks) == 1 else max(4, math.ceil(want * 2.2 / len(blocks)))
        found, ai = brain.find_clips(
            bw, db[s0:s1], (b - a) / 16000, job["title"], job["uploader"],
            want if len(blocks) == 1 else cand_target, int(o["min_len"]), int(o["max_len"]), notes.append,
            lean=True, focus=job.get("focus", ""), spike=spike[s0:s1], visual=visual,
            want_candidates=cand_target, lang=selected_lang or job.get("audio_lang") or job.get("lang") or "",
        )
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        if ai.get("note"):
            notes.append(ai["note"])
        for c in found:
            c.update(start=round(c["start"] + shift, 2), end=round(c["end"] + shift, 2))
            job.setdefault("cands", []).append(c)
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        job["done_blocks"] = bi + 1       # a restart from here on carries on with the next part
        save(job)

    pool = [c for c in job.pop("cands", []) if not any(c["start"] < k["end"] and k["start"] < c["end"] for k in job["clips"])]
    discovery = {}
    if pool and len(job["clips"]) < want:
        pool, pre_dup = brain.deduplicate_clips(pool, words)
        if model:
            _set(job, stage=f"The critic is comparing {len(pool)} moments from the whole video", progress=max(job["progress"], 0.88))
            pool = sorted(pool, key=lambda c: -brain.rank(c))[:max(want * 2, 28)]
            brain.critic(pool, words, job["title"], notes.append, focus=job.get("focus", ""))
        winners, discovery = brain.select_best_clips(
            pool, words, float(job.get("duration") or hi / 16000), want,
            range_start=lo / 16000, range_end=hi / 16000,
            quality_threshold=56.0 if model else 0.0,
        )
        discovery["deduplicated_out"] = discovery.get("deduplicated_out", 0) + pre_dup
        if winners and model:
            _set(job, stage=f"Writing hooks and titles for {len(winners)} clips", progress=max(job["progress"], 0.90))
            brain.describe_many(winners, words, job["title"], job["uploader"], notes.append,
                                lang=selected_lang or job.get("audio_lang") or job.get("lang") or "")
        for c in sorted(winners, key=lambda c: c["start"]):
            take(c)
        job["discovery"] = discovery
        job["shortfall_note"] = discovery.get("shortfall_reason") or ""
        save(job)
    if o.get("fill_requested") and len(job["clips"]) < want:
        # Fill remaining requested slots ONLY when explicitly requested via API option fill_requested=True.
        relative_words = [dict(w, s=w["s"] - lo / 16000, e=w["e"] - lo / 16000)
                          for w in words if lo / 16000 <= w["s"] < hi / 16000]
        ranked = db.copy()
        for c in job["clips"]:
            first = max(0, int(c["start"] - lo / 16000 - o["max_len"]))
            last = max(first, int(c["end"] - lo / 16000 + o["max_len"]) + 1)
            ranked[first:last] = -120
        minimum, maximum = int(o["min_len"]), int(o["max_len"])
        for c in brain.fallback_by_loudness(ranked, relative_words, want * 2, maximum, span, spike=spike, min_len=minimum):
            c["start"] += lo / 16000
            c["end"] += lo / 16000
            if c["end"] - c["start"] < minimum:
                continue
            if any(c["start"] < prior["end"] and prior["start"] < c["end"] for prior in job["clips"]):
                continue
            take(c)
            if len(job["clips"]) >= want:
                break
        save(job)
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    for f in futures:
        f.result()
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    renders.shutdown()
    ahead.shutdown(wait=False)
    fetch.shutdown(wait=False)
    del audio
    import gc
    gc.collect()                      # lets go of the audio file so it can be deleted
    try:
        wav.unlink(missing_ok=True)
    except OSError:
        pass
    clips = [c for c in job["clips"] if c["status"] == "done"]
    shortfall = job.get("shortfall_note") or ""
    if clips and not model:
        ai_note = "The AI was unavailable, so clips were picked by audio/visual peaks only."
        if shortfall:
            ai_note += " " + shortfall
    else:
        ai_note = shortfall
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:], "note": ai_note}
    stage = "Done" if clips else "No 1080p clips were rendered"
    _set(job, status="done", stage=stage, progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        try:
            if kind == "job":
                _run_job(job)
            else:
                words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
                clip = next(c for c in job["clips"] if c["id"] == cid)
                render_one(job, clip, words)
        except Exception as e:  # noqa: BLE001
            if kind == "job" and (isinstance(e, Cancelled) or job.get("cancel")):
                with _lock:
                    job.update(cancel=False, cancelled=True)
                    job["clips"] = [c for c in job["clips"] if c.get("status") == "done"]   # finished clips are kept
                _set(job, status="done", stage="Cancelled by you", error=None, progress=1.0)
                creators.log(job["creator"], f"Video cancelled: {job.get('title') or ''}")
                continue
            (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            if kind == "job":
                for c in job["clips"]:
                    if c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error="The video stopped before this clip was made.")
                _set(job, status="error", error=safe.friendly(e), stage="Stopped")
                creators.log(job["creator"], f"Video failed: {safe.friendly(e)[:200]}")
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    def resume_serial(job, ids):
        with ctx.as_user(job["user"]):
            d = Path(job["dir"])
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
            for cid in ids:
                clip = next((c for c in job["clips"] if c["id"] == cid), None)
                if not clip:
                    continue
                if clip.get("src") or Path(job.get("source_path") or "").is_file():
                    render_one(job, clip, words)
                else:
                    clip.update(status="rendering", stage="Fetching the 1080p source")
                    save(job)
                    _asked_piece(job, clip)
                    clip.pop("stage", None)
                    save(job)
            if job.get("stage", "").startswith("Rebuilding portrait Reels"):
                _set(job, status="done", stage="Done", progress=1.0)
    for j in all_jobs():
        remakes = j.pop("remake", [])
        if remakes and j.get("options", {}).get("fast_render"):
            submit(lambda job=j, ids=remakes: resume_serial(job, ids))
        else:
            for cid in remakes:
                _q.put(("clip", j["id"], cid))
        if j.get("status") == "done":
            save(j)


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
