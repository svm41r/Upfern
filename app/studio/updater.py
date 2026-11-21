"""Updates: Upfern checks an optional configured feed for a newer version and installs it with one press."""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import httpx

from . import config

FEED = config.env("SHABICLIPS_UPDATE_FEED", "").strip()
OK = (".py", ".html", ".css", ".js", ".json", ".txt", ".png", ".svg", ".ico", ".woff2", ".ttf", ".otf", ".webp", ".jpg")
_cache = {"at": 0.0, "info": None}


def version() -> str:
    f = config.ROOT / "VERSION"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def check(force=False) -> dict:
    """{"current", "latest", "available", "notes"}; disabled unless SHABICLIPS_UPDATE_FEED is set."""
    cur = version()
    if not FEED:
        return {"current": cur, "latest": cur, "available": False, "notes": "", "changes": []}
    if force or time.time() - _cache["at"] > 600 or _cache["info"] is None:
        try:
            r = httpx.get(FEED, timeout=10, headers={"Cache-Control": "no-cache"}, params={"t": int(time.time())})
            r.raise_for_status()
            _cache["info"] = r.json()
        except Exception:  # noqa: BLE001 - offline or the site is down: no update shown
            _cache["info"] = _cache["info"] or {}
        _cache["at"] = time.time()
    info = _cache["info"] or {}
    latest = str(info.get("version") or "")
    changes = [{"version": str(c.get("version")), "notes": str(c.get("notes") or "")[:600]}
               for c in (info.get("changes") or []) if isinstance(c, dict) and str(c.get("version") or "") > cur][:20]
    return {"current": cur, "latest": latest, "available": bool(latest) and latest > cur, "notes": info.get("notes") or "",
            "changes": changes or ([{"version": latest, "notes": info.get("notes") or ""}] if latest > cur else [])}


def install_zip(raw: bytes) -> int:
    """Install a Upfern-Update.zip: only the app's own code is replaced; old files go to data/update-backup."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a zip file. Pick the Upfern-Update.zip you were sent.")
    names = [n for n in z.namelist() if not n.endswith("/")]
    if not any(n.split("/")[-1] in ("shabiclips-update.json", "clipkar-update.json") for n in names):
        raise ValueError("That zip isn't a Upfern update.")
    plan = []
    for n in names:
        rel = n[4:] if n.startswith("app/") else n
        parts = rel.split("/")
        if ".." in parts or rel.startswith("/") or ":" in rel or "\\" in rel:
            raise ValueError("That update has a bad file path in it, so it was not installed.")
        if rel == "VERSION" or (parts[0] in ("studio", "web") and rel.lower().endswith(OK)):
            plan.append((n, rel))
    if not plan:
        raise ValueError("That update has nothing in it to install.")
    import re as _re
    backup = config.DATA_DIR / "update-backup" / (_re.sub(r"[^\w.\-]", "_", version())[:40] or "old")
    failed = []
    for n, rel in plan:
        dest = config.ROOT / rel
        data = z.read(n)
        if dest.is_file():
            try:                    # a backup of the old file: contents only (copying dates fails in OneDrive folders)
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dest, backup / rel)
            except OSError:
                pass                # no backup is not a reason to stop the update
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)   # all at once, so a file is never left half written
        except OSError:
            try:
                dest.write_bytes(data)
            except OSError as e:
                failed.append(f"{rel} ({e.strerror or e})")
    if failed:
        raise ValueError(f"{len(failed)} file(s) couldn't be written: {failed[0]}. If Upfern is in a OneDrive folder "
                         "(Desktop or Documents), move the Upfern folder to C:/ShabiClips and try again.")
    return len(plan)


def download_and_install() -> dict:
    if not FEED:
        raise ValueError("No update to install right now.")
    info = _cache["info"] if _cache["info"] else None
    if not info:
        check(force=True)
        info = _cache["info"] or {}
    url, want = info.get("url"), str(info.get("sha256") or "").lower()
    if not url or not want or not url.startswith("https://"):
        raise ValueError("No update to install right now.")
    r = httpx.get(url, timeout=120, follow_redirects=False)
    r.raise_for_status()
    raw = r.content
    if len(raw) > 60_000_000 or hashlib.sha256(raw).hexdigest() != want:
        raise ValueError("The update didn't download properly. Try again in a minute.")
    n = install_zip(raw)
    return {"installed": n, "version": version()}


def restart_soon(delay=1.5):
    """Start a fresh Upfern a moment after this one closes, then close this one."""
    def go():
        time.sleep(delay)
        exe = sys.executable
        if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"
        # a small helper waits until this ClipKar has closed, then opens it again (no console, no quoting trouble)
        helper = ("import time, subprocess, sys; time.sleep(3); "
                  f"subprocess.Popen([sys.executable, '-m', 'studio.desktop'], cwd={str(config.ROOT)!r})")
        subprocess.Popen([exe, "-c", helper], cwd=str(config.ROOT), creationflags=0x00000008 | 0x00000200)   # detached, own group
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()
