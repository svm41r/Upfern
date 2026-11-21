"""Finished-clip reviews, shared by automatic scoring and editor rechecks."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ai, brain, ctx, usage

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-review")
_pending = set()
_retry_after = {}


class NoSpeech(ValueError):
    pass


class Changed(ValueError):
    pass


def _key(job, clip):
    return job["id"], clip["id"], clip.get("version", 0)


def _revision(clip):
    return (clip.get("version", 0), clip["start"], clip["end"], clip.get("hook") or "",
            tuple(map(tuple, clip.get("cuts") or ())))


def review(job, clip):
    """Store one transcript-based score, provided the rendered revision still matches."""
    from . import jobs

    if clip.get("status") != "done":
        raise Changed("Wait for this clip to finish rendering.")
    version, start, end, hook, cuts = _revision(clip)
    words = clip.get("fixed_words") or []
    if not words:
        path = Path(job["dir"]) / "words.json"
        if path.is_file():
            words = json.loads(path.read_text(encoding="utf-8"))
    text = " ".join(str(w.get("w", "")) for w in words
                    if start - .2 <= w["s"] <= end + .2 and not any(a <= w["s"] < b for a, b in cuts))
    if len(text.split()) < 6:
        raise NoSpeech("There is too little speech to score this clip.")
    usage.set_context(job.get("creator"), job.get("title") or "")
    length = end - start - sum(max(0, min(b, end) - max(a, start)) for a, b in cuts)
    result = brain.performance(text, hook, job.get("title") or "", job.get("uploader") or "", length)
    with jobs._lock:
        if (clip.get("status") != "done" or _revision(clip) != (version, start, end, hook, cuts)
                or not any(c is clip for c in job["clips"])):
            raise Changed("This clip changed during review. Its new version will be scored automatically.")
        clip["perf"] = result
        clip.pop("score_status", None)
        jobs.save(job)
    return result


def _finish(job, clip, key):
    from . import jobs

    try:
        with ctx.as_user(job["user"]):
            try:
                review(job, clip)
            except NoSpeech:
                if _key(job, clip) == key and clip.get("status") == "done":
                    clip["score_status"] = "no_speech"
                    jobs.save(job)
            except Changed:
                pass
            except Exception:
                with _lock:
                    _retry_after[key] = time.monotonic() + 300
                if _key(job, clip) == key and clip.get("status") == "done":
                    clip["score_status"] = "unavailable"
                    jobs.save(job)
    finally:
        with _lock:
            _pending.discard(key)


def queue(job, clip):
    if (clip.get("status") != "done" or clip.get("perf") is not None
            or clip.get("score_status") == "no_speech" or not ai.ready()):
        return False
    key = _key(job, clip)
    with _lock:
        now = time.monotonic()
        if key in _pending or len(_pending) >= 6 or _retry_after.get(key, 0) > now:
            return False
        _pending.add(key)
        for expired in [k for k, at in _retry_after.items() if at <= now]:
            _retry_after.pop(expired, None)
    _pool.submit(_finish, job, clip, key)
    return True


def queue_jobs(items):
    from . import jobs

    for item in items:
        job = jobs.get(item["id"])
        if job is None:
            continue
        for clip in job["clips"]:
            queue(job, clip)
            with _lock:
                if len(_pending) >= 6:
                    return


def checking(job, clip):
    with _lock:
        return _key(job, clip) in _pending
