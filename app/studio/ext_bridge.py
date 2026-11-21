"""Chrome Extension bridge & Documents export engine for 1-click multi-platform social posting."""
import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from . import config, creators, ctx, jobs

_lock = threading.RLock()
_SETTINGS_FILE = config.DATA_DIR / "ext_settings.json"
_QUEUE_FILE = config.DATA_DIR / "ext_queue.json"
_MAX_QUEUE = 120

_hb = {"last_seen": 0.0, "version": "", "ext_id": ""}

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")
PLATFORM_LABELS = {
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "facebook": "Facebook Reels",
}
PLATFORM_URLS = {
    "youtube": "https://studio.youtube.com/",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/reels/create",
}
_PLATFORM_URL_DOMAINS = {
    "channel_url": (("youtube.com",), "YouTube Studio"),
    "tiktok_url": (("tiktok.com",), "TikTok"),
    "instagram_url": (("instagram.com",), "Instagram"),
    "facebook_url": (("facebook.com",), "Facebook"),
}


def normalize_platforms(raw, fallback: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Normalize a single platform name, comma string, or sequence into ordered valid platform keys."""
    if isinstance(raw, str):
        items = [x.strip().lower() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x or "").strip().lower() for x in raw]
    else:
        items = []
    out: list[str] = []
    for p in items:
        if p in PLATFORMS and p not in out:
            out.append(p)
    if out:
        return out
    if fallback:
        fb = [str(p).strip().lower() for p in fallback if str(p).strip().lower() in PLATFORMS]
        if fb:
            return list(dict.fromkeys(fb))
    return ["youtube", "tiktok", "instagram", "facebook"]


def _item_platform(item: dict) -> str:
    p = str(item.get("platform") or "").strip().lower()
    return p if p in PLATFORMS else "youtube"


def _default_export_dir() -> Path:
    docs = None
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(0, 5, 0, 0, buf) == 0 and buf.value:
                docs = Path(buf.value)
        except Exception:  # noqa: BLE001
            docs = None
    if not docs:
        docs = Path.home() / "Documents"
    return docs / "Upfern"


def extension_dir() -> Path:
    up = config.ROOT.parent / "Upfern-Extension"
    return up if up.is_dir() else config.ROOT.parent / "ShabiClips-Extension"


def _default_settings() -> dict:
    return {
        "export_dir": str(_default_export_dir()),
        "channel_url": "",
        "tiktok_url": "",
        "instagram_url": "",
        "facebook_url": "",
        "default_platforms": ["youtube", "tiktok", "instagram", "facebook"],
        "privacy": "public",
        "made_for_kids": False,
        "add_shorts_tag": True,
        "title_add_shorts": False,
        "auto_publish": True,
        "auto_open_chrome": True,
        "chrome_profile": "",
        "save_meta_txt": False,
    }


def _read_json(path: Path, default):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return default


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def settings() -> dict:
    with _lock:
        saved = _read_json(_SETTINGS_FILE, {})
        base = _default_settings()
        if isinstance(saved, dict):
            for k in base:
                if k in saved and saved[k] is not None:
                    base[k] = saved[k]
        if not str(base["export_dir"]).strip():
            base["export_dir"] = str(_default_export_dir())
        base["default_platforms"] = normalize_platforms(base.get("default_platforms"), fallback=["youtube", "tiktok", "instagram", "facebook"])
        return base


def _clean_platform_url(raw: str, allowed_domains: tuple[str, ...], label: str) -> str:
    u = str(raw or "").strip()[:300]
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    host = (urlparse(u).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in allowed_domains):
        raise ValueError(f"{label} URL must be a {allowed_domains[0]} link.")
    return u


def _clean_channel_url(raw: str) -> str:
    return _clean_platform_url(raw, ("youtube.com",), "Channel")


def save_settings(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("Settings payload must be an object.")
    with _lock:
        cur = settings()
        if "export_dir" in patch:
            raw = str(patch["export_dir"] or "").strip()
            if raw:
                p = Path(raw).expanduser()
                if not p.is_absolute():
                    raise ValueError("Export folder must be a full folder path (for example C:\\Users\\You\\Documents\\Shabi Clips).")
                p.mkdir(parents=True, exist_ok=True)
                cur["export_dir"] = str(p)
            else:
                cur["export_dir"] = str(_default_export_dir())
        for field, (domains, label) in _PLATFORM_URL_DOMAINS.items():
            if field in patch:
                cur[field] = _clean_platform_url(patch[field], domains, label)
        if "default_platforms" in patch:
            cur["default_platforms"] = normalize_platforms(patch["default_platforms"], fallback=["youtube", "tiktok", "instagram", "facebook"])
        if "privacy" in patch:
            priv = str(patch["privacy"] or "public").strip().lower()
            cur["privacy"] = priv if priv in ("public", "unlisted", "private") else "public"
        for bkey in ("made_for_kids", "add_shorts_tag", "title_add_shorts", "auto_publish", "auto_open_chrome", "save_meta_txt"):
            if bkey in patch:
                cur[bkey] = bool(patch[bkey])
        if "chrome_profile" in patch:
            cur["chrome_profile"] = re.sub(r"[\r\n\x00]", "", str(patch["chrome_profile"] or "").strip())[:80]
        _write_json(_SETTINGS_FILE, cur)
        return cur


# ------------------------------------------------------------------ Chrome & Explorer helpers
def _find_chrome() -> str:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    if sys.platform == "win32":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                pass
    return shutil.which("chrome") or shutil.which("google-chrome") or ""


def platform_target_url(platform: str, st: dict | None = None) -> str:
    """Return the configured upload URL for a social platform, or its built-in default."""
    st = st or settings()
    p = platform if platform in PLATFORMS else "youtube"
    field_map = {
        "youtube": "channel_url",
        "tiktok": "tiktok_url",
        "instagram": "instagram_url",
        "facebook": "facebook_url",
    }
    custom = (st.get(field_map[p]) or "").strip()
    if custom:
        if p == "youtube" and "studio.youtube.com" not in custom.lower():
            return PLATFORM_URLS["youtube"]
        return custom
    return PLATFORM_URLS[p]


def studio_target_url(st: dict | None = None) -> str:
    return platform_target_url("youtube", st)


def open_chrome(urls: str | list[str] | tuple[str, ...] | None = None, profile: str | None = None) -> tuple[bool, str]:
    st = settings()
    if urls is None:
        url_list = [studio_target_url(st)]
    elif isinstance(urls, str):
        url_list = [urls] if urls.strip() else [studio_target_url(st)]
    else:
        url_list = list(dict.fromkeys(str(u).strip() for u in urls if str(u or "").strip()))
        if not url_list:
            url_list = [studio_target_url(st)]
    prof = st.get("chrome_profile", "") if profile is None else profile
    chrome = _find_chrome()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    if chrome:
        args = [chrome]
        if prof:
            args.append(f"--profile-directory={prof}")
        args.extend(url_list)
        try:
            subprocess.Popen(args, close_fds=True, creationflags=flags)
            return True, chrome
        except OSError:
            pass
    try:
        for target in url_list:
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606 - local trusted social studio URL
            else:
                webbrowser.open(target)
        return True, "default-browser"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def open_in_explorer(kind: str = "export") -> str:
    if kind == "extension":
        target = extension_dir()
        if not target.is_dir():
            raise ValueError("ShabiClips-Extension folder was not found next to app.")
    else:
        target = Path(settings()["export_dir"])
        target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", str(target)], close_fds=True)
    else:
        webbrowser.open(target.as_uri())
    return str(target)


# ------------------------------------------------------------------ Heartbeat
def record_heartbeat(version: str = "", ext_id: str = "") -> dict:
    with _lock:
        _hb["last_seen"] = time.time()
        if version:
            _hb["version"] = str(version)[:32]
        if ext_id:
            _hb["ext_id"] = str(ext_id)[:64]
    return brief()


def is_connected() -> bool:
    with _lock:
        return bool(_hb["last_seen"] and (time.time() - _hb["last_seen"]) < 90)


def brief() -> dict:
    st = settings()
    u = ctx.uid()
    with _lock:
        uq = [q for q in _load_queue() if q.get("user") == u]
        by_platform = {
            p: sum(1 for x in uq if x.get("status") == "pending" and _item_platform(x) == p)
            for p in PLATFORMS
        }
        return {
            "connected": is_connected(),
            "last_seen": _hb["last_seen"],
            "version": _hb["version"],
            "ext_id": _hb["ext_id"],
            "export_dir": st["export_dir"],
            "ext_dir": str(extension_dir()),
            "default_platforms": st.get("default_platforms") or ["youtube"],
            "platforms": list(PLATFORMS),
            "by_platform": by_platform,
            "pending": sum(1 for x in uq if x.get("status") in ("pending", "uploading", "filling")),
        }


# ------------------------------------------------------------------ Queue & Export
def _load_queue() -> list:
    data = _read_json(_QUEUE_FILE, [])
    return data if isinstance(data, list) else []


def _save_queue(items: list):
    if len(items) > _MAX_QUEUE:
        items = items[-_MAX_QUEUE:]
    _write_json(_QUEUE_FILE, items)


def _sanitize_filename(title: str, clip_id: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    if not clean:
        clean = "ShabiClip"
    clean = clean[:72].rstrip(" .")
    cid_short = re.sub(r"[^\w\-]", "", str(clip_id or ""))[:16] or secrets.token_hex(3)
    return f"{clean}_{cid_short}.mp4"


def _parse_hashtags(raw) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[\s,]+", raw)
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = []
    out, seen = [], set()
    for p in parts:
        tag = re.sub(r"[^A-Za-z0-9_]", "", p.lstrip("#").strip())
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag[:40])
    return out[:25]


def _trim_title(title: str, suffix: str = "", limit: int = 100) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip() or "Short Clip"
    if not suffix:
        return t[:limit].rstrip()
    budget = limit - len(suffix)
    if len(t) <= budget:
        return f"{t}{suffix}"
    cut = t[:budget].rstrip()
    if " " in cut and len(cut) > 20:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return f"{cut}{suffix}"


def _build_social_caption(title: str, description: str, hashtags: list[str], footer: str = "") -> str:
    """Build a platform-agnostic caption for TikTok, Instagram Reels, and Facebook Reels."""
    clean_title = re.sub(r"#shorts\b", "", str(title or ""), flags=re.I).strip()
    desc_body = str(description or "").strip()
    parts: list[str] = []
    if clean_title and clean_title.lower() not in desc_body.lower():
        parts.append(clean_title)
    if desc_body:
        parts.append(desc_body)
    social_tags = [h for h in hashtags if h.lower() != "shorts"]
    if social_tags:
        parts.append(" ".join("#" + h for h in social_tags))
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)[:2150]


def _build_youtube_meta(clip: dict, creator: dict, st: dict, overrides: dict | None = None) -> dict:
    ov = overrides if isinstance(overrides, dict) else {}
    raw_title = ov.get("title") if ov.get("title") is not None else (clip.get("title") or clip.get("hook") or "Short Clip")
    clean_raw_title = re.sub(r"\s+", " ", str(raw_title)).strip() or "Short Clip"
    if st.get("title_add_shorts") and "#shorts" not in clean_raw_title.lower():
        title = _trim_title(clean_raw_title, " #Shorts", 100)
    else:
        title = _trim_title(clean_raw_title, "", 100)

    raw_hashtags = _parse_hashtags(ov.get("hashtags") if ov.get("hashtags") is not None else clip.get("hashtags", []))
    hashtags = list(raw_hashtags)
    if st.get("add_shorts_tag", True) and all(h.lower() != "shorts" for h in hashtags):
        hashtags.insert(0, "Shorts")

    desc_body = str(ov.get("description") if ov.get("description") is not None else (clip.get("description") or "")).strip()
    footer = str(creator.get("post_footer") or "").strip()
    hash_line = " ".join("#" + h for h in hashtags)
    description = "\n\n".join(x for x in (desc_body, hash_line, footer) if x)[:4950]
    caption = _build_social_caption(clean_raw_title, desc_body, raw_hashtags, footer)

    # Comma-joined video tags for YouTube Studio Details -> Show more -> Tags (500 char ceiling)
    tag_candidates = [*hashtags, "shorts", "youtubeshorts", "clips"]
    tags, seen_tags, total_len = [], set(), 0
    for tg in tag_candidates:
        low = tg.lower()
        if not tg or low in seen_tags:
            continue
        add_len = len(tg) + (1 if tags else 0)
        if total_len + add_len > 480:
            break
        seen_tags.add(low)
        tags.append(tg)
        total_len += add_len

    privacy = str(ov.get("privacy") or st.get("privacy") or creator.get("privacy") or "public").strip().lower()
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"

    made_for_kids = bool(ov["made_for_kids"]) if "made_for_kids" in ov else bool(st.get("made_for_kids", False))
    auto_publish = bool(ov["auto_publish"]) if "auto_publish" in ov else bool(st.get("auto_publish", False))
    dur = max(0.0, float(clip.get("end", 0) or 0) - float(clip.get("start", 0) or 0))
    is_short = (clip.get("aspect") or "9:16") == "9:16" or dur <= 180

    return {
        "title": title,
        "description": description,
        "caption": caption,
        "hashtags": hashtags,
        "tags": tags,
        "privacy": privacy,
        "made_for_kids": made_for_kids,
        "auto_publish": auto_publish,
        "is_short": is_short,
        "duration_sec": round(dur, 2),
    }


def _export_clip_file(src: Path, export_dir: Path, filename: str, meta: dict, save_txt: bool) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    dest = export_dir / filename
    try:
        tmp = export_dir / (filename + ".part")
        shutil.copy2(src, tmp)
        tmp.replace(dest)
    except OSError:
        # Fallback if another process holds `dest` open: pick numbered filename
        stem, suf = dest.stem, dest.suffix
        for idx in range(2, 50):
            alt = export_dir / f"{stem} ({idx}){suf}"
            if not alt.exists():
                shutil.copy2(src, alt)
                dest = alt
                break
        else:
            shutil.copy2(src, dest)
    if save_txt:
        txt_path = dest.with_suffix(".txt")
        lines = [
            f"Title: {meta['title']}",
            f"Privacy: {meta['privacy']}",
            f"Made for Kids: {'Yes' if meta['made_for_kids'] else 'No'}",
            f"Tags: {', '.join(meta['tags'])}",
            "",
            "Description (YouTube):",
            meta["description"],
            "",
            "Caption (TikTok / Instagram / Facebook):",
            meta.get("caption") or meta["description"],
        ]
        txt_path.write_text("\n".join(lines), encoding="utf-8")
    return dest


def _mirror_clip(job: dict, clip: dict, item: dict, save: bool = True):
    status_map = {
        "pending": "scheduled",
        "uploading": "posting",
        "filling": "posting",
        "ready": "posting",
        "posted": "posted",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    plat = _item_platform(item)
    clip.setdefault("posts", {})[item["id"]] = {
        "account_name": f"{PLATFORM_LABELS[plat]} (Ext)",
        "platform": plat,
        "status": status_map.get(item["status"], "scheduled"),
        "url": item.get("url") or "",
        "due_at": item.get("created_at") or time.time(),
        "error": item.get("error"),
    }
    if save:
        jobs.save(job)


def _enqueue_for_platforms(
    job: dict,
    clip: dict,
    creator: dict,
    st: dict,
    plats: list[str],
    overrides: dict | None = None,
) -> tuple[list[dict], Path]:
    if not clip or clip.get("status") != "done" or not clip.get("file"):
        raise ValueError("That clip is not finished rendering yet.")
    src = Path(job["dir"]) / "clips" / Path(str(clip["file"])).name
    if not src.is_file():
        raise ValueError("The rendered clip video file could not be found on disk.")

    meta = _build_youtube_meta(clip, creator or creators.DEFAULTS, st, overrides=overrides)
    filename = _sanitize_filename(meta["title"], clip["id"])
    export_dir = Path(st["export_dir"])
    dest = _export_clip_file(src, export_dir, filename, meta, bool(st.get("save_meta_txt")))
    size_bytes = dest.stat().st_size

    now = time.time()
    created: list[dict] = []
    with _lock:
        q = _load_queue()
        for plat in plats:
            item = {
                "id": "extq_" + secrets.token_hex(6),
                "platform": plat,
                "platform_label": PLATFORM_LABELS[plat],
                "target_url": platform_target_url(plat, st),
                "user": ctx.uid(),
                "job": job["id"],
                "clip": clip["id"],
                "creator": job.get("creator") or "",
                "creator_name": (creator or {}).get("name") or "",
                "status": "pending",
                "stage": f"Exported to {dest.name}",
                "progress": 0,
                "filename": dest.name,
                "export_path": str(dest),
                "source_path": str(src),
                "size_bytes": size_bytes,
                "channel_url": st.get("channel_url") or "",
                "url": "",
                "error": None,
                "created_at": now,
                "updated_at": now,
                **meta,
            }
            q.append(item)
            created.append(item)
        _save_queue(q)

    for idx, item in enumerate(created):
        _mirror_clip(job, clip, item, save=(idx == len(created) - 1))
    return created, dest


def queue_clip(
    job: dict,
    clip: dict,
    creator: dict | None = None,
    overrides: dict | None = None,
    open_browser: bool = True,
    platforms=None,
) -> dict:
    st = settings()
    c = creator or creators.get(job.get("creator")) or creators.DEFAULTS
    plats = normalize_platforms(platforms, fallback=st.get("default_platforms") or ["youtube"])
    created, dest = _enqueue_for_platforms(job, clip, c, st, plats, overrides=overrides)
    chrome_opened, chrome_note = (False, "")
    if open_browser and st.get("auto_open_chrome", True):
        urls = [platform_target_url(p, st) for p in plats]
        chrome_opened, chrome_note = open_chrome(urls, st.get("chrome_profile", ""))
    pub_items = [_public_item(it) for it in created]
    return {
        "ok": True,
        "item": pub_items[0],
        "items": pub_items,
        "platforms": plats,
        "count": len(pub_items),
        "export_path": str(dest),
        "export_dir": str(dest.parent),
        "chrome_opened": chrome_opened,
        "chrome_note": chrome_note,
        "ext_connected": is_connected(),
    }


def queue_batch(
    entries: list[tuple[dict, dict, dict]],
    overrides: dict | None = None,
    open_browser: bool = True,
    platforms=None,
) -> dict:
    if not entries:
        raise ValueError("No finished clips were selected.")
    st = settings()
    plats = normalize_platforms(platforms, fallback=st.get("default_platforms") or ["youtube"])
    items, paths = [], []
    for job, clip, creator in entries:
        created, dest = _enqueue_for_platforms(job, clip, creator or creators.DEFAULTS, st, plats, overrides=overrides)
        items.extend(_public_item(it) for it in created)
        paths.append(str(dest))
    chrome_opened, chrome_note = (False, "")
    if open_browser and st.get("auto_open_chrome", True):
        urls = [platform_target_url(p, st) for p in plats]
        chrome_opened, chrome_note = open_chrome(urls, st.get("chrome_profile", ""))
    return {
        "ok": True,
        "count": len(entries),
        "clip_count": len(entries),
        "queued_count": len(items),
        "platforms": plats,
        "items": items,
        "export_paths": paths,
        "export_dir": st["export_dir"],
        "chrome_opened": chrome_opened,
        "chrome_note": chrome_note,
        "ext_connected": is_connected(),
    }


def _public_item(item: dict) -> dict:
    out = {k: v for k, v in item.items() if k not in ("user", "source_path")}
    plat = _item_platform(item)
    out["platform"] = plat
    out.setdefault("platform_label", PLATFORM_LABELS[plat])
    return out


def list_queue(limit: int = 30, platform: str | None = None) -> list[dict]:
    u = ctx.uid()
    p_filter = str(platform or "").strip().lower()
    if p_filter and p_filter not in PLATFORMS:
        p_filter = ""
    with _lock:
        q = [
            x for x in _load_queue()
            if x.get("user") == u and (not p_filter or _item_platform(x) == p_filter)
        ]
    q.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return [_public_item(x) for x in q[:limit]]


def get_item(qid: str) -> dict | None:
    if not re.fullmatch(r"(?:ytq|extq)_[0-9a-f]{12}", str(qid or "")):
        return None
    u = ctx.uid()
    with _lock:
        for x in _load_queue():
            if x.get("id") == qid and x.get("user") == u:
                return dict(x)
    return None


def next_pending(platform: str | None = None, claim: bool = True) -> dict | None:
    u = ctx.uid()
    p_filter = str(platform or "").strip().lower()
    if p_filter and p_filter not in PLATFORMS:
        p_filter = ""
    with _lock:
        q = _load_queue()
        target = next(
            (
                x for x in q
                if x.get("user") == u
                and x.get("status") == "pending"
                and (not p_filter or _item_platform(x) == p_filter)
            ),
            None,
        )
        if not target:
            return None
        if claim:
            plat = _item_platform(target)
            target["status"] = "uploading"
            target["stage"] = f"Connecting to {PLATFORM_LABELS[plat]}"
            target["updated_at"] = time.time()
            target["error"] = None
            _save_queue(q)
            job = jobs.get(target["job"])
            clip = next((c for c in job["clips"] if c["id"] == target["clip"]), None) if job else None
            if job and clip:
                _mirror_clip(job, clip, target)
        return _public_item(target)


def update_status(qid: str, patch: dict) -> dict:
    allowed = ("pending", "uploading", "filling", "ready", "posted", "done", "failed", "cancelled")
    with _lock:
        q = _load_queue()
        u = ctx.uid()
        target = next((x for x in q if x.get("id") == qid and x.get("user") == u), None)
        if not target:
            raise KeyError("Queue item not found.")
        if "status" in patch:
            s = str(patch["status"]).strip().lower()
            if s not in allowed:
                raise ValueError("Invalid queue status.")
            if s == "done":
                s = "posted"
            target["status"] = s
        if "stage" in patch and patch["stage"] is not None:
            target["stage"] = str(patch["stage"])[:140]
        if "progress" in patch and patch["progress"] is not None:
            target["progress"] = max(0, min(100, int(float(patch["progress"]))))
        if "url" in patch and patch["url"] is not None:
            target["url"] = str(patch["url"]).strip()[:300]
        if "error" in patch:
            target["error"] = str(patch["error"])[:300] if patch["error"] else None
        elif target["status"] == "posted":
            target["error"] = None
        target["updated_at"] = time.time()
        _save_queue(q)
        job = jobs.get(target["job"])
        clip = next((c for c in job["clips"] if c["id"] == target["clip"]), None) if job else None
        if job and clip:
            _mirror_clip(job, clip, target)
        return _public_item(target)


def retry_item(qid: str, open_browser: bool = True) -> dict:
    cur = get_item(qid)
    plat = _item_platform(cur or {})
    item = update_status(qid, {"status": "pending", "stage": f"Waiting for {PLATFORM_LABELS[plat]}", "progress": 0, "error": None})
    st = settings()
    if open_browser and st.get("auto_open_chrome", True):
        open_chrome([platform_target_url(plat, st)], st.get("chrome_profile", ""))
    return item


def cancel_item(qid: str) -> dict:
    return update_status(qid, {"status": "cancelled", "stage": "Cancelled by user"})


def delete_item(qid: str) -> bool:
    u = ctx.uid()
    with _lock:
        q = _load_queue()
        kept = [x for x in q if not (x.get("id") == qid and x.get("user") == u)]
        if len(kept) == len(q):
            return False
        _save_queue(kept)
    ctx.bump()
    return True


def video_path(qid: str) -> Path:
    item = get_item(qid)
    if not item:
        raise KeyError("Queue item not found.")
    exp = Path(item.get("export_path") or "")
    if exp.is_file():
        return exp
    src = Path(item.get("source_path") or "")
    if src.is_file():
        return src
    job = jobs.get(item["job"])
    clip = next((c for c in job["clips"] if c["id"] == item["clip"]), None) if job else None
    if job and clip and clip.get("file"):
        fallback = Path(job["dir"]) / "clips" / Path(str(clip["file"])).name
        if fallback.is_file():
            return fallback
    raise FileNotFoundError("Clip MP4 file is missing.")
