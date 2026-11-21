"""Licenses: sign in with Google, then a license key. Checked against the owner's Supabase project.

1. "Continue with Google" opens the normal web browser at Supabase's Google sign-in. Google sends it back to this
   copy's own address (/license/callback), which hands the sign-in to ClipKar.
2. The person types the license key the owner gave them. Supabase (clipkar_check2) ties the key to that Google
   account on first use, and to at most N computers.
3. The copy checks in again every few hours, and on each start. A key or a computer the owner turned off locks it.
   Without internet a copy keeps working for 3 days after its last good check.

There is no owner mode in the app: everyone, the owner included, signs in and uses a key. What the owner may see
and change is decided by Supabase (row level security on the owner's Google email), in the separate owner website."""
import hashlib
import json
import platform
import secrets
import subprocess
import threading
import time

import httpx

from . import config

GRACE = 3 * 24 * 3600          # works this long without internet after its last good check
EVERY = 4 * 3600               # checks in this often
_lock = threading.RLock()
_state: dict = {}


def _file():
    return config.DATA_DIR / "license.json"


def url() -> str:
    return config.SUPABASE_URL.rstrip("/")


def anon() -> str:
    return config.SUPABASE_ANON_KEY


def enabled() -> bool:
    # the test suite runs ClipKar without a license; the switch is read from the process only, never from .env
    import os
    if os.environ.get("CLIPKAR_TEST_RUN") == "1" and os.environ.get("PEAKCUT_DATA"):
        return False
    return bool(url() and anon()) and not config.ACCOUNTS


def device_id() -> str:
    """This computer, the same every time (Windows' own machine id, hashed), so a reinstall is not a new computer."""
    raw = ""
    try:
        out = subprocess.run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"], capture_output=True,
                             text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        raw = out.strip().split()[-1] if "MachineGuid" in out else ""
    except Exception:  # noqa: BLE001
        raw = ""
    if not raw:
        raw = _load().get("rnd") or secrets.token_hex(16)
        _save({"rnd": raw})
    return hashlib.sha256(("clipkar:" + raw).encode()).hexdigest()[:32]


def _load() -> dict:
    with _lock:
        if not _state:
            try:
                _state.update(json.loads(_file().read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return dict(_state)


def _save(patch: dict):
    with _lock:
        _load()
        _state.update(patch)
        for k in [k for k, v in _state.items() if v is None]:
            _state.pop(k)
        try:
            _file().write_text(json.dumps(_state), encoding="utf-8")
        except OSError:
            pass


def _counts():
    try:
        from . import jobs
        js = jobs.list_jobs()
        return len(js), sum(len(j.get("clips") or []) for j in js)
    except Exception:  # noqa: BLE001
        return 0, 0


# ------------------------------------------------------------------ Google sign-in, through Supabase
def google_url(back: str) -> str:
    from urllib.parse import quote
    return f"{url()}/auth/v1/authorize?provider=google&redirect_to={quote(back, safe='')}&prompt=select_account"


def set_session(access: str, refresh: str) -> dict:
    """The sign-in handed back by Supabase: checked with Supabase before it is kept."""
    r = httpx.get(f"{url()}/auth/v1/user", headers={"apikey": anon(), "Authorization": f"Bearer {access}"}, timeout=15)
    if r.status_code != 200:
        raise ValueError("That sign-in didn't work. Try Continue with Google again.")
    u = r.json()
    meta = u.get("user_metadata") or {}
    _save({"access": access, "refresh": refresh, "email": u.get("email") or "", "name": meta.get("full_name") or meta.get("name") or "",
           "avatar": meta.get("avatar_url") or "", "ok": False, "reason": ""})
    if _load().get("key"):
        return check()
    return {"ok": False, "reason": ""}


def _fresh_token() -> str:
    """A usable sign-in token: renewed with the refresh token when the old one has run out (they last an hour)."""
    st = _load()
    tok = st.get("access") or ""
    try:
        import base64
        exp = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "==")).get("exp", 0)
    except Exception:  # noqa: BLE001
        exp = 0
    if tok and exp - time.time() > 120:
        return tok
    if not st.get("refresh"):
        return ""
    r = httpx.post(f"{url()}/auth/v1/token?grant_type=refresh_token", json={"refresh_token": st["refresh"]},
                   headers={"apikey": anon()}, timeout=15)
    if r.status_code != 200:
        if r.status_code in (400, 401):
            _save({"access": None, "refresh": None})       # signed out somewhere else, or too old: sign in again
            return ""
        r.raise_for_status()
    d = r.json()
    _save({"access": d["access_token"], "refresh": d.get("refresh_token") or st["refresh"]})
    return d["access_token"]


def sign_out():
    _save({"access": None, "refresh": None, "ok": False, "email": "", "name": "", "avatar": ""})


# ------------------------------------------------------------------ the license check
def check(key=None) -> dict:
    """Ask Supabase about this copy's key, as the signed-in Google account. Saves and returns {ok, reason}."""
    st = _load()
    key = (key or st.get("key") or "").strip().upper()
    if not key:
        return {"ok": False, "reason": "Enter your license key."}
    try:
        tok = _fresh_token()
    except Exception as e:  # noqa: BLE001 - offline: the last good answer stands, within the grace time
        _save({"checked_at": time.time(), "net_error": str(e)[-120:]})
        return {"ok": allowed(), "reason": "Can't reach the license server. Check your internet.", "offline": True}
    if not tok:
        _save({"ok": False, "reason": "Your Google sign-in expired.", "checked_at": time.time(), "net_error": ""})
        return {"ok": False, "reason": "Your Google sign-in expired."}
    videos, clips = _counts()
    body = {"p_key": key, "p_device": device_id(), "p_pc": f"{platform.node()} · Windows {platform.release()}",
            "p_version": _version(), "p_clips": clips, "p_videos": videos}
    try:
        r = httpx.post(f"{url()}/rest/v1/rpc/clipkar_check2", json=body, timeout=15,
                       headers={"apikey": anon(), "Authorization": f"Bearer {tok}"})
        r.raise_for_status()
        ans = r.json()
    except Exception as e:  # noqa: BLE001
        _save({"checked_at": time.time(), "net_error": str(e)[-120:]})
        return {"ok": allowed(), "reason": "Can't reach the license server. Check your internet.", "offline": True}
    ok = bool(ans.get("ok"))
    patch = {"ok": ok, "reason": ans.get("reason") or "", "label": ans.get("label") or "", "checked_at": time.time(), "net_error": ""}
    if ok:
        patch.update(key=key, ok_at=time.time())
    _save(patch)
    return {"ok": ok, "reason": patch["reason"]}


def allowed() -> bool:
    """May this copy be used right now? Signed in, with a key that passed a check in the last 3 days."""
    if not enabled():
        return True
    st = _load()
    return bool(st.get("key")) and bool(st.get("ok")) and bool(st.get("refresh")) and time.time() - float(st.get("ok_at") or 0) < GRACE


def state() -> dict:
    st = _load()
    return {"enabled": enabled(), "allowed": allowed(), "signed_in": bool(st.get("refresh")), "email": st.get("email", ""),
            "name": st.get("name", ""), "avatar": st.get("avatar", ""), "has_key": bool(st.get("key")),
            "key_end": (st.get("key") or "")[-4:], "reason": st.get("reason", ""), "offline": bool(st.get("net_error"))}


def _version():
    f = config.ROOT / "VERSION"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def loop():
    """Check in on start and every few hours, in the background."""
    def run():
        time.sleep(5)
        while True:
            st = _load()
            if enabled() and st.get("key") and st.get("refresh"):
                try:
                    check()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(EVERY)
    threading.Thread(target=run, daemon=True, name="license").start()
