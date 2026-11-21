"""Upfern web server. Run with:  python -m studio.server

Public pages:  /  /login  /signup          The app:  /app  (signed-in users only)
Everything under /api, /media and /oauth needs a session, except /api/auth/* and /api/public."""
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import sqlalchemy as sa
import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import admin as admins
from . import account, load
from . import (accounts, ai, alerts, auth, config, creators, ctx, db, jobs, live, media, posting,
               profiles, render, safe, transcribe, usage)
from . import brain, ext_bridge, faces
from . import license as lic
from . import updater
from . import sfx

app = FastAPI(title="Upfern", docs_url=None, redoc_url=None, openapi_url=None)
WEB = config.ROOT / "web"
OPEN = ("/api/auth/", "/pub/", "/api/public", "/auth/google/", "/auth/verify", "/brand/", "/assets/", "/healthz", "/favicon.ico", "/robots.txt", "/sitemap.xml")
PAGES = {"/": "landing.html", "/login": "auth.html", "/signup": "auth.html", "/reset": "reset.html",
         "/privacy": "legal.html", "/terms": "legal.html", "/acceptable-use": "legal.html"}
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts", ".flv"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
       "frame-src https://player.twitch.tv https://www.youtube-nocookie.com https://player.kick.com; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://accounts.google.com")


BOOT = f"{int(time.time()) % 100000:x}"     # part of every sync token, so pages reload their data after a restart


@app.on_event("startup")
async def _more_threads():
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = int(config.env("PEAKCUT_REQUEST_THREADS", "64"))


@app.on_event("startup")
def _startup():
    db.init()
    for f in (config.TMP_DIR / "uploads").glob("*"):      # uploads cut short by a restart
        try:
            f.unlink()
        except OSError:
            pass
    render.install_fonts()
    jobs.start()
    live.start()
    posting.start()
    lic.loop()


# ================================================================ the gate: who is asking, and are they allowed
class Gate:
    """Runs before every request: finds the signed-in user, blocks what needs sign-in, blocks cross-site form
    posts, caps upload size and adds security headers."""

    def __init__(self, app):
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        req = Request(scope)
        path, method = scope["path"], scope["method"]
        origin = req.headers.get("origin") or ""
        is_ext_api = path.startswith("/api/ext/")
        is_ext_origin = origin.startswith("chrome-extension://")

        if is_ext_api and method == "OPTIONS":
            preflight_headers = {
                "Access-Control-Allow-Origin": origin if is_ext_origin else "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Range, X-Shabi-Ext, X-Upfern-Ext, X-ClipKar-Ext",
                "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
                "Access-Control-Max-Age": "600",
                "Cache-Control": "private, no-store",
            }
            resp = Response(status_code=204, headers=preflight_headers)
            await resp(scope, receive, send)
            return

        async def deny(status, msg, redirect=None):
            no_keep = {"Cache-Control": "private, no-store"}      # never kept by a browser or CDN
            if is_ext_api and is_ext_origin:
                no_keep["Access-Control-Allow-Origin"] = origin
                no_keep["Access-Control-Expose-Headers"] = "Content-Range, Content-Length, Accept-Ranges"
            resp = RedirectResponse(redirect, 303, headers=no_keep) if redirect else JSONResponse({"detail": msg}, status, headers=no_keep)
            await resp(scope, receive, send)

        if method not in ("GET", "HEAD", "OPTIONS"):
            if origin and urlparse(origin).netloc != req.headers.get("host") and origin.rstrip("/") != config.PUBLIC_URL:
                if not (is_ext_api and is_ext_origin):
                    return await deny(403, "Cross-site requests are not allowed.")
            # Only the upload may be big. Everything else is a small form, and is cut off at 256 KB whatever its
            # headers claim, so nobody can fill the server's memory by posting junk (signed in or not).
            piece = method == "PUT" and path.startswith("/api/upload/")
            cap = ((min(config.MAX_UPLOAD_MB, config.UPLOAD_PART_MB) if piece else config.MAX_UPLOAD_MB) + 8) * 1024 * 1024 \
                if path == "/api/upload" or piece else 60 * 1024 * 1024 if path in ("/api/update", "/api/outro") else 256 * 1024
            if path in ("/api/cover/generate", "/api/cover/cutout") or re.fullmatch(r"/api/jobs/[^/]+/clips/[^/]+/memes", path):
                cap = 8 * 1024 * 1024
            try:
                if int(req.headers.get("content-length") or 0) > cap:
                    return await deny(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB." if path == "/api/upload" else "That request is too large.")
            except ValueError:
                return await deny(400, "Bad request.")
            seen, inner_receive = 0, receive

            async def receive():
                nonlocal seen
                m = await inner_receive()
                if m["type"] == "http.request":
                    seen += len(m.get("body", b""))
                    if seen > cap:
                        raise HTTPException(413, "That request is too large.")
                return m

        is_open = path in PAGES or any(path.startswith(p) for p in OPEN)
        admin_area = path == "/admin" or path.startswith(("/admin/", "/api/admin/"))
        if config.ACCOUNTS:
            cookie = req.cookies.get(auth.COOKIE, "")
            user = auth._sess.get(auth._h(cookie)) if cookie and not admin_area else None      # remembered: no database, no thread hop
            user = user[1] if user and time.time() - user[0] < auth.SESS_FOR else (
                await run_in_threadpool(auth.user_for, cookie, admin_area) if cookie else None)
        else:
            # No sign-in: whoever opens the page is the owner. Because nothing else stands in the way, the page
            # must have been asked for by its real address (a number, "localhost", or the address in settings),
            # so another website cannot reach it through a made-up name that points at this machine.
            if not _own_address(req.headers.get("host", "")):
                return await deny(403, "Open Upfern by its own address.")
            if path == "/reset" or path.startswith("/auth/"):
                return await deny(303, "", redirect="/app")
            if path.startswith("/api/auth/") and path != "/api/auth/me":
                return await deny(404, "Not Found")
            user = auth._local or await run_in_threadpool(auth.local_user)
            # a license is needed to use this copy (the owner's copy, with the service key, never needs one)
            if not lic.allowed() and not (path in ("/license", "/license/callback", "/favicon.ico", "/healthz") or path.startswith(("/api/license", "/api/update/", "/brand/", "/static/", "/fonts/"))):
                return await deny(402, "Upfern needs a license key.", redirect=None if path.startswith(("/api/", "/media/")) else "/license")
        if admin_area:
            # The admin area does not exist for anyone else: same "not found" whether signed out or a normal user.
            if not user or not user.is_admin:
                return await deny(404, "Not Found")
            if method not in ("GET", "HEAD"):
                # changes must come from our own pages: the browser has to say so, and it has to match
                origin = (req.headers.get("origin") or "").rstrip("/")
                if not origin or (urlparse(origin).netloc != req.headers.get("host") and origin != config.PUBLIC_URL):
                    return await deny(403, "Cross-site requests are not allowed.")
        if not user and not is_open:
            if path == "/app" or path.startswith("/app/"):
                return await deny(401, "", redirect="/login")
            return await deny(401, "Please sign in.")
        if user and path in ("/login", "/signup"):
            return await deny(303, "", redirect="/app")
        scope.setdefault("state", {})["user"] = user
        if user and path in ("/api/upload", "/api/upload/start") and method == "POST":
            # decided before a single byte of the file is accepted: verified, has a free slot, has disk space
            def allowed():
                with ctx.as_user(user.id):
                    _need_verified(); _job_slots(); _quota()                     # noqa: E702
            try:
                await run_in_threadpool(allowed)
            except HTTPException as e:
                return await deny(e.status_code, e.detail)
        if user and path.startswith("/api/") and not auth.seen_recently(user.id):
            await run_in_threadpool(auth.touch, user.id)

        unchanged = False

        async def send_with_headers(message):
            nonlocal unchanged
            if unchanged:                       # the browser's copy is current: send no body at all
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    await send({"type": "http.response.body", "body": b""})
                return
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                if method == "GET" and message.get("status") == 200:
                    tag = next((v.decode("latin-1") for k, v in h if k.lower() == b"etag"), "")
                    have = req.headers.get("if-none-match", "")
                    if tag and have and tag.removeprefix("W/") in [x.strip().removeprefix("W/") for x in have.split(",")]:
                        unchanged = True
                        message["status"] = 304
                        h[:] = [(k, v) for k, v in h if k.lower() not in (b"content-length", b"content-type", b"content-encoding")]
                h += [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                      (b"referrer-policy", b"strict-origin-when-cross-origin"), (b"content-security-policy", CSP.encode()),
                      (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                if config.HTTPS:
                    h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if path.startswith("/api/"):
                    h.append((b"cache-control", b"no-store"))
                elif message.get("status", 200) >= 300 and not any(k.lower() == b"cache-control" for k, _ in h):
                    # redirects and errors are never kept by a CDN: a "not found" for a clip that is still being
                    # made, or a sign-in redirect, must not be shown to anyone later
                    h.append((b"cache-control", b"private, no-store"))
                if path.startswith(("/app", "/admin", "/api/", "/auth/", "/reset", "/media/")):
                    h.append((b"x-robots-tag", b"noindex, nofollow"))
                if is_ext_api and is_ext_origin:
                    h += [(b"access-control-allow-origin", origin.encode("latin-1")),
                          (b"access-control-expose-headers", b"Content-Range, Content-Length, Accept-Ranges")]
            await send(message)

        token = ctx._uid.set(user.id if user else None)
        try:
            if user and path == "/api/sync" and method == "GET":
                # By far the most common request. When nothing of this user's changed it is answered right here:
                # no worker thread, no database, no disk. Anything else falls through to the normal route.
                quick = _sync_unchanged(req.query_params.get("v", ""))
                if quick is not None:
                    return await JSONResponse(quick)(scope, receive, send_with_headers)
            await self.inner(scope, receive, send_with_headers)
        finally:
            ctx._uid.reset(token)


app.add_middleware(Gate)


class Compress:
    """gzip for text responses. Videos and images are already compressed and need byte ranges, so they pass through."""
    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware
        self.plain, self.gzip = app, GZipMiddleware(app, minimum_size=800, compresslevel=6)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        media = path.startswith(("/media/", "/api/upload")) or (path.startswith("/api/ext/") and path.endswith("/video")) or path.endswith((".mp4", ".jpg", ".png", ".webp", ".ico"))
        await (self.plain if scope["type"] != "http" or media else self.gzip)(scope, receive, send)


app.add_middleware(Compress)


# Cloudflare's published address ranges (cloudflare.com/ips). PEAKCUT_CDN_RANGES replaces them if they ever change.
_CDN_RANGES = None


def _from_cdn(addr: str) -> bool:
    global _CDN_RANGES
    import ipaddress
    if _CDN_RANGES is None:
        ranges = config.env("PEAKCUT_CDN_RANGES") or (
            "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 "
            "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 "
            "104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 "
            "2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32")
        _CDN_RANGES = [ipaddress.ip_network(r) for r in ranges.replace(",", " ").split()]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in _CDN_RANGES)


def _ip(request: Request) -> str:
    """The visitor's address, used for rate limits. The connection's own address, or the one the proxy in front
    reports (PEAKCUT_TRUST_PROXY). If that connection comes from the CDN, the CDN's header names the real visitor
    (PEAKCUT_CLIENT_IP_HEADER). The header is believed only when the CDN really sent the request, so nobody can
    pick their own address by sending it themselves."""
    fwd = request.headers.get("x-forwarded-for", "")
    peer = (fwd.split(",")[0].strip() if fwd and config.env("PEAKCUT_TRUST_PROXY") else "") or (request.client.host if request.client else "")
    named = config.env("PEAKCUT_CLIENT_IP_HEADER")
    if named and request.headers.get(named) and _from_cdn(peer):
        return request.headers[named].split(",")[0].strip()[:64]
    return peer


def _own_address(host: str) -> bool:
    """Is this the address Upfern is really reached by? A number (127.0.0.1, 192.168.1.20), "localhost", or the
    host in PEAKCUT_PUBLIC_URL."""
    import ipaddress
    name = host.strip().lower()
    name = name[1:name.index("]")] if name.startswith("[") and "]" in name else name.rsplit(":", 1)[0] if ":" in name else name
    if name in ("localhost", urlparse(config.PUBLIC_URL).hostname or "") or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _me(request: Request):
    return request.state.user


def _admin(request: Request):
    """The signed-in admin, read from the database for this very request. Anyone else gets "not found"."""
    u = getattr(request.state, "user", None)
    if not u or not u.is_admin or u.disabled:
        raise HTTPException(404)
    return u


def _need_verified():
    if not auth.is_verified(ctx.uid()):
        raise HTTPException(403, "Confirm your email first. We sent you a link when you signed up; you can send a new one from the banner at the top.")


# ================================================================ pages
def _page(name, shared=False):
    """A page. The browser keeps its copy and asks each time whether it has changed; when it has not, the answer
    is a few bytes ("not modified") instead of the whole page again. Signed-in pages are never kept by anything
    in between (a proxy or CDN), only by the person's own browser."""
    return FileResponse(WEB / name, headers={"Cache-Control": ("public" if shared else "private") + ", no-cache"})


_LANDING: dict = {}


@app.get("/")
def landing():
    f = WEB / "landing.html"
    m = f.stat().st_mtime
    if _LANDING.get("m") != m:
        html = f.read_text(encoding="utf-8").replace("{{SITE}}", config.PUBLIC_URL)
        _LANDING.update(m=m, html=html, etag='"' + hashlib.md5(html.encode()).hexdigest() + '"')
    # fresh for five minutes; after that a CDN or browser may show its copy while it quietly fetches the new one
    return HTMLResponse(_LANDING["html"], headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=86400",
                                                   "ETag": _LANDING["etag"]})


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /api/\nDisallow: /auth/\nDisallow: /reset\nDisallow: /media/\n"
                    f"\nSitemap: {config.PUBLIC_URL}/sitemap.xml\n", media_type="text/plain",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sitemap.xml")
def sitemap():
    day = time.strftime("%Y-%m-%d")
    pages = [("/", "1.0", "weekly"), ("/signup", "0.8", "monthly"), ("/login", "0.4", "monthly"), ("/terms", "0.3", "yearly"),
             ("/privacy", "0.3", "yearly"), ("/acceptable-use", "0.2", "yearly")]
    body = "".join(f"<url><loc>{config.PUBLIC_URL}{u}</loc><lastmod>{day}</lastmod><changefreq>{c}</changefreq>"
                   f"<priority>{p}</priority></url>" for u, p, c in pages)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + body + "</urlset>", media_type="application/xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/login")
@app.get("/signup")
def auth_page():
    return _page("auth.html", shared=True)


@app.get("/reset")
def reset_page():
    return _page("reset.html", shared=True)


LEGAL = {
    "privacy": ("Privacy Policy", "How Upfern collects, uses and protects your data, including Google user data, cookies and your privacy rights."),
    "terms": ("Terms of Service", "The terms for using Upfern, the AI clipping tool for streams and videos: accounts, availability and liability."),
    "acceptable-use": ("Acceptable Use Policy", "What you may and may not do with Upfern, the AI stream clipping tool."),
}


@app.get("/privacy")
@app.get("/terms")
@app.get("/acceptable-use")
def legal_page(request: Request):
    """Public, so Google's app review (and anyone) can read them. Business name, law and contact come from settings."""
    import html as _h
    page = request.url.path.strip("/")
    title, desc = LEGAL[page]
    contact = config.CONTACT_EMAIL
    who = (f'<a href="mailto:{_h.escape(contact)}">{_h.escape(contact)}</a>' if contact
           else "the contact details on our website")
    name = _h.escape(config.LEGAL_NAME or "Upfern")
    law = (f"the laws of {_h.escape(config.LEGAL_COUNTRY)}" if config.LEGAL_COUNTRY
           else "the laws of the country where Upfern's operator is established")
    out = (_LEGAL_HTML or (WEB / "legal.html").read_text(encoding="utf-8"))
    for k, v in {"{{CONTACT}}": who, "{{PAGE}}": page, "{{PAGE_PATH}}": page, "{{SITE}}": _h.escape(config.PUBLIC_URL),
                 "{{NAME}}": name, "{{LAW}}": law, "{{TITLE}}": title, "{{DESC}}": desc}.items():
        out = out.replace(k, v)
    return HTMLResponse(out, headers={"Cache-Control": "public, max-age=3600"})


_LEGAL_HTML = ""


@app.get("/license")
def license_page():
    if lic.allowed():
        return RedirectResponse("/app", 303)
    return _page("license.html")


@app.get("/license/callback")
def license_callback():
    """Where Google sign-in comes back to (in the normal web browser): the page hands the sign-in to Upfern."""
    return _page("license_callback.html")


@app.get("/api/license")
def license_state():
    return lic.state()


@app.post("/api/license/google")
def license_google(request: Request):
    """Open Google sign-in in the normal web browser (Google doesn't allow signing in inside app windows)."""
    import webbrowser
    back = str(request.base_url).rstrip("/") + "/license/callback"
    u = lic.google_url(back)
    try:
        webbrowser.open(u)
    except Exception:  # noqa: BLE001 - the page opens it itself
        pass
    return {"url": u}


@app.post("/api/license/session")
def license_session(body: dict = Body(...)):
    _throttle("licsess:" + ctx.uid(), 20, 600, "Too many tries. Wait a few minutes.")
    access, refresh = str(body.get("access") or "")[:4000], str(body.get("refresh") or "")[:400]
    if not access or not refresh:
        raise HTTPException(400, "The sign-in came back empty. Try again.")
    try:
        lic.set_session(access, refresh)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return lic.state()


@app.post("/api/license/signout")
def license_signout():
    lic.sign_out()
    return lic.state()


@app.post("/api/license")
def license_activate(body: dict = Body(...)):
    _throttle("lic:" + ctx.uid(), 10, 600, "Too many tries. Wait a few minutes.")
    key = str(body.get("key") or "").strip()[:40]
    if not key:
        raise HTTPException(400, "Type your license key.")
    if not lic.state()["signed_in"]:
        raise HTTPException(401, "Sign in with Google first.")
    r = lic.check(key)
    if not r["ok"]:
        raise HTTPException(403, r["reason"] or "That key didn't work.")
    return lic.state()


@app.get("/app/edit/{jid}/{cid}")
def editor_page(jid: str, cid: str):
    return _page("editor.html")


@app.get("/app")
@app.get("/app/{rest:path}")
def app_page(rest: str = ""):
    """Every app address (/app/videos, /app/creators/<id>/setup ...) is the same page; it shows the right view."""
    return _page("index.html")


@app.get("/admin")
@app.get("/admin/{rest:path}")
def admin_page(request: Request, rest: str = ""):
    _admin(request)
    return _page("admin.html")


@app.get("/healthz")
def healthz():
    with db.engine.connect() as c:
        c.execute(sa.text("select 1"))
    return {"ok": True, "app": "upfern"}


@app.get("/brand/{name}")
def brand(name: str):
    if name == "upfern.webmanifest":
        return FileResponse(WEB / "brand" / name, media_type="application/manifest+json")
    if not re.fullmatch(r"[\w\-]+\.(svg|png|ico|webp|jpg|mp4)", name) or not (WEB / "brand" / name).is_file():
        raise HTTPException(404)
    return FileResponse(WEB / "brand" / name, headers={"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"})


@app.get("/assets/upfern-autopost.zip")
def desktop_extension_download():
    archive = WEB / "upfern-autopost.zip"
    if not archive.is_file():
        raise HTTPException(404)
    return FileResponse(archive, media_type="application/zip", filename=archive.name)

@app.get("/assets/{name}")
def asset(name: str):
    if not re.fullmatch(r"[\w\-]+\.(?:js|css)", name) or not (WEB / name).is_file():
        raise HTTPException(404)
    mime = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(WEB / name, media_type=mime, headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"})


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/brand/favicon.svg?v=8", 308)


# ================================================================ sign up, sign in, sign out
@app.get("/api/public")
def public_config():
    from . import mailer
    return {"google": auth.google_ready(), "mail": mailer.ready(), "verify_email": config.VERIFY_EMAIL}


def _signed_in(request: Request, user, first: bool, redirect=None, how="password"):
    token = auth.start_session(user.id, _ip(request), request.headers.get("user-agent", ""))
    try:        # the admin's live page; a sign-in never fails over it
        with db.engine.begin() as c:
            c.execute(db.signins.insert().values(t=time.time(), user_id=user.id, how=how, first=bool(first), ip=_ip(request)[:64],
                                                 agent=request.headers.get("user-agent", "")[:300]))
    except Exception:  # noqa: BLE001
        pass
    resp = RedirectResponse(redirect, 303) if redirect else JSONResponse({"user": auth.public(user)})
    resp.set_cookie(**auth.cookie_args(token))
    return resp


@app.post("/api/auth/signup")
def signup(request: Request, body: dict = Body(...)):
    try:
        auth.throttle("signup:" + _ip(request), 8, 3600)
        user, first = auth.signup(str(body.get("email", "")), str(body.get("password", "")), str(body.get("name", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    if not auth.is_verified(user.id):
        auth.send_verify(user)
    return _signed_in(request, user, first, how="signup")


@app.post("/api/auth/resend-verification")
def resend_verification(request: Request):
    u = _me(request)
    if not u:
        raise HTTPException(401, "Please sign in.")
    if auth.is_verified(u.id):
        return {"verified": True}
    try:
        auth.throttle("verify:" + u.id, 3, 900)
    except auth.AuthError:
        raise HTTPException(429, "We've sent a few already. Check your spam folder, or try again in 15 minutes.")
    from . import mailer
    return {"sent": auth.send_verify(u), "mail_ready": mailer.ready()}


@app.get("/auth/verify")
def verify_email(request: Request, t: str = ""):
    try:
        auth.verify_link(t)
    except auth.AuthError as e:
        return RedirectResponse("/app?verify_error=" + quote(str(e)), status_code=303)
    return RedirectResponse("/app?verified=1", status_code=303)


@app.post("/api/auth/forgot")
def forgot_password(request: Request, body: dict = Body(...)):
    email = str(body.get("email", "")).strip().lower()
    try:
        auth.throttle("forgot-ip:" + _ip(request), 10, 3600)
        auth.throttle("forgot:" + email, 3, 3600)
    except auth.AuthError as e:
        raise HTTPException(429, str(e))
    auth.forgot(email)
    return {"ok": True}   # the same answer whether or not the account exists


@app.post("/api/auth/reset")
def reset_password(request: Request, body: dict = Body(...)):
    try:
        auth.throttle("reset-ip:" + _ip(request), 20, 3600)
        user = auth.reset_password(str(body.get("token", "")), str(body.get("password", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, user, False, how="reset")


@app.post("/api/auth/login")
def login(request: Request, body: dict = Body(...)):
    email = str(body.get("email", ""))
    try:
        auth.throttle("login-ip:" + _ip(request), 30, 600)
        auth.throttle(f"login:{email.strip().lower()[:254]}:{_ip(request)}", 8, 600)
        auth.throttle("login:" + email.strip().lower()[:254], 60, 600)
        user = auth.login(email, str(body.get("password", "")))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return _signed_in(request, user, False)


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(request: Request):
    return {"user": auth.public(_me(request)) if _me(request) else None}


@app.post("/api/auth/password")
def change_password(request: Request, body: dict = Body(...)):
    if not _me(request):
        raise HTTPException(401, "Please sign in.")
    try:
        auth.throttle("pw:" + _me(request).id, 6, 600)
        auth.change_password(_me(request).id, str(body.get("current", "")), str(body.get("new", "")),
                             keep_token=request.cookies.get(auth.COOKIE, ""))
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/auth/google/start")
def google_start(next: str = "/app"):
    if not auth.google_ready():
        raise HTTPException(404)
    url, state = auth.google_url(next)
    resp = RedirectResponse(url)
    # the sign-in can only be finished by the browser that started it
    resp.set_cookie("g_state", state, max_age=900, httponly=True, samesite="lax", secure=config.HTTPS, path="/auth/google")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state or not hmac.compare_digest(request.cookies.get("g_state", ""), state):
        return RedirectResponse("/login?error=google", 303)
    try:
        auth.throttle("google:" + _ip(request), 30, 600)
        user, first, nxt = auth.google_finish(code, state)
    except auth.AuthError:
        return RedirectResponse("/login?error=google", 303)
    return _signed_in(request, user, first, redirect=nxt, how="google")


# ================================================================ app status
@app.get("/api/status")
def status(request: Request):
    from . import notices
    return {"user": auth.public(_me(request)), "accounts": config.ACCOUNTS, "links": {"repo": config.REPO_URL, "follow": config.FOLLOW_URL}, "ai": usage.brief(), "unread": notices.unread(),
            "tour": not accounts.settings()["tour_done"],
            "youtube": accounts.youtube_ready(), "connect": {p: accounts.can_connect(p) for p in accounts.PLATFORM_NAMES}, "max_upload_mb": config.MAX_UPLOAD_MB, "upload_part_mb": config.UPLOAD_PART_MB, "max_batch": config.MAX_ACTIVE_JOBS_PER_USER,
            "styles": {k: v["label"] for k, v in render.STYLES.items()}, "layouts": render.LAYOUTS, "languages": config.LANGUAGES,
            "ext": ext_bridge.brief()}


# ================================================================ videos
def _job_or_404(jid: str):
    job = jobs.get(jid) if re.fullmatch(r"[\w\-]{8,60}", jid) else None
    if not job:
        raise HTTPException(404, "No such video")
    return job


def _job_slots():
    if jobs.busy_count() >= config.MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(429, f"You already have {config.MAX_ACTIVE_JOBS_PER_USER} videos in progress. Wait for one to finish.")


_create_lock = threading.Lock()      # "do I have a free slot?" and "take it" happen together, so bursts cannot slip past the limit
_lookups = threading.BoundedSemaphore(8)   # link and channel previews running at once, whole server
_disk: dict = {}


def _quota():
    """Each account has a fixed amount of disk space. Measured at most once a minute."""
    if load.disk_full():
        raise HTTPException(503, "Upfern is out of space for new videos for the moment. Please try again shortly.")
    uid = ctx.uid()
    got = _disk.get(uid)
    if not got or time.time() - got[0] > 60:
        total = 0
        for root, _dirs, files in os.walk(config.user_dir(uid)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        got = _disk[uid] = (time.time(), total)
    if got[1] > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "Your storage is full. Delete some older videos to make room.")


def _throttle(key, limit, window, msg="Too many requests at once. Wait a moment."):
    try:
        auth.throttle(key, limit, window)
    except auth.AuthError:
        raise HTTPException(429, msg)


def _lookup(fn, *a):
    """Run a preview lookup, unless the server is already doing as many as it should."""
    if not _lookups.acquire(blocking=False):
        raise HTTPException(429, "Lots of people are looking things up right now. Try again in a few seconds.")
    try:
        return fn(*a)
    finally:
        _lookups.release()


def _creator_rows():
    out = []
    for c in creators.all_creators():
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
        s = live.active_for(c["id"])
        out.append({**c, "live_job": s.job["id"] if s else None, "log": creators.read_log(c["id"], 30)})
    return out


def _sync_token() -> str:
    return f"{BOOT}.{ctx.version()}.{jobs.queue_epoch() if jobs.busy_count() else 0}"


def _sync_unchanged(v: str):
    """{"v": token} if the page is up to date and this user's creators are already in memory, else None."""
    mine = creators._mem.get(ctx.uid())
    if mine is None or not v or v != _sync_token():
        return None
    for c in list(mine.values()):                      # still keep live status fresh while the page is open
        if profiles.needs_refresh(c):
            profiles.refresh(c["id"])
    return {"v": v}


@app.get("/api/sync")
def sync(v: str = ""):
    """What the page asks every couple of seconds. It sends the token it got last time; when nothing of this
    user's has changed, the answer is just that token: no files read, nothing built."""
    from . import scores
    items = jobs.list_jobs()
    scores.queue_jobs(items)
    token = _sync_token()
    if v == token:
        return _sync_unchanged(v) or {"v": token}
    from . import notices
    return {"v": token, "jobs": [jobs.public(j) for j in items], "creators": _creator_rows(),
            "accounts": accounts.list_accounts(), "ai": usage.brief(), "unread": notices.unread()}


@app.get("/api/jobs")
def list_jobs():
    from . import scores
    items = jobs.list_jobs()
    scores.queue_jobs(items)
    return [jobs.public(j) for j in items]


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return jobs.public(_job_or_404(jid))


@app.get("/api/preview")
def link_preview(request: Request, url: str = ""):
    url = media.normalize(url[:500])
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        auth.throttle(f"preview:{_me(request).id}", 12, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many links at once. Wait a moment.")
    try:
        return _lookup(media.preview, url)
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, media.explain(e))


@app.post("/api/jobs")
def create_from_link(body: dict = Body(...)):
    _need_verified()
    _quota()
    _throttle("newjob:" + ctx.uid(), 30, 600)
    url = media.normalize(str(body.get("url", ""))[:500])
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a full link to a YouTube, Twitch or Kick video.")
    try:
        media.check_youtube(url)          # YouTube is refusing us right now: say so, rather than queue a job that fails
    except media.YouTubeBlocked as e:
        raise HTTPException(503, str(e))
    creator = str(body.get("creator") or "") or None
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    raw_opts = body.get("options") or {}
    seen = None
    sel = None
    if media.is_youtube(url):
        req_track = str(raw_opts.get("audio_track") or "") if isinstance(raw_opts, dict) else ""
        try:
            seen = media._PREVIEWS.get(url) or _lookup(media.preview, url)
            sel = media.select_audio_track(seen, req_track)
        except media.UnverifiedAudioTrackError as e:
            return JSONResponse(status_code=422, content={"detail": e.reason, "code": "unverified_audio_track", "tracks": e.tracks})
        except media.LinkError as e:
            raise HTTPException(422, str(e))
        except media.YouTubeBlocked as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, media.explain(e))
    try:
        with _create_lock:
            _job_slots()
            job = jobs.create({"type": "url", "url": url, "name": url}, raw_opts, creator=creator)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e) if isinstance(e, ValueError) else "Those options aren't valid.")
    seen = seen or media._PREVIEWS.get(url)  # already looked up while the user was typing: show it straight away
    if seen:
        patch = {"title": seen["title"], "uploader": seen["uploader"], "duration": seen["duration"]}
        if sel:
            patch.update(
                audio_track={"id": sel["id"], "lang": sel["lang"], "label": sel["label"],
                             "verified": sel["verified"], "is_dubbed": sel["is_dubbed"], "explicit": sel["explicit"]},
                audio_lang=sel["lang"],
                audio_is_dubbed=sel["is_dubbed"],
            )
            if not job.get("lang") and sel["lang"]:
                patch["lang"] = sel["lang"]
        job.update(patch)
        if media.save_thumb(jobs.job_dir(job["id"]), url=url):
            job["thumb"] = True
        jobs.save(job)
    return jobs.public(job)


@app.post("/api/upload")
def create_from_file(file: UploadFile = File(...), options: str = Form("{}"), creator: str = Form("")):
    _need_verified()
    _job_slots()
    name = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "video.mp4").name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    try:
        opts = json.loads(options)
        opts = opts if isinstance(opts, dict) else {}
    except ValueError:
        opts = {}
    updir = config.TMP_DIR / "uploads"
    updir.mkdir(exist_ok=True)
    tmp = updir / f"{os.urandom(8).hex()}{ext}"
    limit, size = config.MAX_UPLOAD_MB * 1024 * 1024, 0
    try:
        with tmp.open("wb") as f:
            while chunk := file.file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create({"type": "file", "path": str(tmp), "name": name}, opts, enqueue=False, creator=creator or None)
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        _disk.pop(ctx.uid(), None)
        dest = jobs.job_dir(job["id"]) / ("source" + ext)
        shutil.move(str(tmp), dest)
    finally:
        tmp.unlink(missing_ok=True)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


# ---------------------------------------------------------------- big files, sent in pieces
# The page cuts a large file into pieces, sends each one, and asks for them to be joined. Each piece is small enough
# for anything standing in front of the app. Nothing is trusted from the page: the sizes are fixed when the upload
# starts, every piece must be exactly its size, and only the account that started an upload can add to it.
_UP_ID = re.compile(r"[0-9a-f]{24}")
_up_lock = threading.Lock()


def _up_root() -> Path:
    d = config.TMP_DIR / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _up_sweep():
    """Forget uploads that were started and never finished (after six hours)."""
    for d in _up_root().glob("up-*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _up_get(upid: str) -> dict:
    """The upload, only if it belongs to whoever is asking."""
    if not _UP_ID.fullmatch(upid or ""):
        raise HTTPException(404, "No such upload")
    try:
        meta = json.loads((_up_root() / f"up-{upid}" / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "This upload has expired. Start it again.")
    if meta.get("user") != ctx.uid():
        raise HTTPException(404, "No such upload")
    return meta


@app.post("/api/upload/start")
def upload_start(body: dict = Body(...)):
    _need_verified()
    _job_slots()
    _quota()
    _throttle("upstart:" + ctx.uid(), 30, 600)
    name = re.sub(r"[^\w.\- ]", "_", Path(str(body.get("name") or "video.mp4")).name)[:120]
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "Upload a video file (mp4, mov, mkv or webm).")
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise HTTPException(400, "Bad file size.")
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"That file is too big. The limit is {config.MAX_UPLOAD_MB} MB.")
    if _disk.get(ctx.uid(), (0, 0))[1] + size > config.MAX_USER_GB * 1e9:
        raise HTTPException(507, "There isn't room for this file in your storage. Delete some older videos to make room.")
    creator = str(body.get("creator") or "")
    if creator and not creators.get(creator):
        raise HTTPException(404, "No such creator")
    opts = body.get("options") if isinstance(body.get("options"), dict) else {}
    try:
        jobs.clean_options(opts)                 # refused now rather than after the whole file has been sent
    except (ValueError, TypeError):
        raise HTTPException(400, "Those options aren't valid.")
    _up_sweep()
    mine = 0
    for d in _up_root().glob("up-*"):
        try:
            mine += json.loads((d / "meta.json").read_text(encoding="utf-8")).get("user") == ctx.uid()
        except (OSError, ValueError):
            pass
    if mine >= 4:
        raise HTTPException(429, "You already have several uploads in progress. Let one finish first.")
    part = config.UPLOAD_PART_MB * 1024 * 1024
    upid = os.urandom(12).hex()
    d = _up_root() / f"up-{upid}"
    d.mkdir()
    meta = {"user": ctx.uid(), "name": name, "ext": ext, "size": size, "part": part, "parts": -(-size // part),
            "options": opts, "creator": creator, "t": time.time()}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"id": upid, "part": part, "parts": meta["parts"]}


@app.put("/api/upload/{upid}/{n}")
async def upload_part(request: Request, upid: str, n: int):
    meta = await run_in_threadpool(_up_get, upid)
    if not 0 <= n < meta["parts"]:
        raise HTTPException(400, "No such piece.")
    want = meta["part"] if n < meta["parts"] - 1 else meta["size"] - meta["part"] * (meta["parts"] - 1)
    d = _up_root() / f"up-{upid}"
    tmp = d / f"{n:05d}.{os.urandom(4).hex()}.tmp"
    got = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > want:
                    raise HTTPException(400, "That piece is the wrong size.")
                await run_in_threadpool(f.write, chunk)
        if got != want:
            raise HTTPException(400, "That piece is the wrong size.")
        tmp.replace(d / f"{n:05d}.part")           # sending a piece again simply replaces it
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "n": n}


@app.post("/api/upload/{upid}/finish")
def upload_finish(upid: str):
    meta = _up_get(upid)
    _need_verified()
    d = _up_root() / f"up-{upid}"
    with _up_lock:                                    # two "finish" calls for one upload make one video, not two
        if not (d / "meta.json").is_file():
            raise HTTPException(404, "This upload has expired. Start it again.")
        parts = [d / f"{i:05d}.part" for i in range(meta["parts"])]
        missing = [i for i, p in enumerate(parts) if not p.is_file()]
        if missing:
            raise HTTPException(409, f"Piece {missing[0] + 1} of {meta['parts']} has not arrived yet.")
        if sum(p.stat().st_size for p in parts) != meta["size"]:
            raise HTTPException(400, "The pieces do not add up to the file.")
        try:
            with _create_lock:
                _job_slots()
                job = jobs.create({"type": "file", "path": "", "name": meta["name"]}, meta["options"], enqueue=False,
                                  creator=meta["creator"] or None)
        except (ValueError, TypeError):
            raise HTTPException(400, "Those options aren't valid.")
        dest = jobs.job_dir(job["id"]) / ("source" + meta["ext"])
        try:
            with dest.open("wb") as out:
                for p in parts:
                    with p.open("rb") as src:
                        shutil.copyfileobj(src, out, 4 * 1024 * 1024)
        except OSError:
            jobs.delete(job["id"])
            raise HTTPException(507, "There wasn't room to save this file. Delete some older videos and try again.")
        shutil.rmtree(d, ignore_errors=True)
    _disk.pop(ctx.uid(), None)
    job["source"]["path"] = str(dest)
    jobs.enqueue(job)
    return jobs.public(job)


@app.delete("/api/upload/{upid}")
def upload_cancel(upid: str):
    _up_get(upid)
    shutil.rmtree(_up_root() / f"up-{upid}", ignore_errors=True)
    return {"ok": True}


@app.post("/api/jobs/{jid}/clips/{cid}/render")
def rerender(jid: str, cid: str, body: dict = Body(...)):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    # only a clip that has never been made yet has to wait; a finished clip can be changed any time, even while the
    # rest of its video is still being worked on, and a change sent while it re-renders is made right after
    if clip.get("status") in ("queued", "rendering") and not clip.get("file") and not clip.get("version"):
        raise HTTPException(409, "This clip is still being made for the first time. Try again in a minute.")
    _throttle("render:" + ctx.uid(), 20, 600, "That's a lot of re-makes in a short time. Wait a few minutes.")
    try:
        jobs.request_render(jid, cid, body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return jobs.public(job)


# ---------------------------------------------------------------- the editor
def _clip_or_404(jid: str, cid: str):
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    src = Path(clip["src"]) if clip.get("src") else Path(job.get("source_path") or "")
    return job, clip, src


@app.get("/api/jobs/{jid}/clips/{cid}/edit")
def clip_edit_info(jid: str, cid: str):
    """Everything the editor needs to show a clip before it is made again: the source, its size, the words."""
    job, clip, src = _clip_or_404(jid, cid)
    off = clip.get("src_offset", 0.0)
    has = src.is_file()
    info = media.probe(src) if has else {}
    words = []
    wf = Path(job["dir"]) / "words.json"
    if clip.get("words_edited") or clip.get("fixed_words"):
        words = clip.get("fixed_words") or []
    elif wf.is_file():
        words = json.loads(wf.read_text(encoding="utf-8"))
    lo, hi = clip["start"] - 40, clip["end"] + 40
    return {"job": jobs.public(job), "clip": cid, "source": has, "offset": off,
            "src_duration": info.get("duration") or 0, "width": info.get("width") or job.get("width") or 0,
            "height": info.get("height") or job.get("height") or 0,
            "words": [{"w": str(w.get("w", "")), "s": w["s"], "e": w["e"],
                       **{k: 1 for k in ("gs", "br") if w.get(k)}} for w in words if lo <= w["s"] <= hi],
            "heard": clip.get("heard_by") == "whisper",
            "aspects": {k: list(v) for k, v in render.ASPECTS.items()}}


@app.get("/media/{jid}/{cid}/source")
def clip_source(jid: str, cid: str):
    """The video a clip was cut from, for the editor's preview. Sent in ranges, so seeking is instant."""
    _, _, src = _clip_or_404(jid, cid)
    if not src.is_file():
        raise HTTPException(404, "The original video is no longer kept.")
    if src.suffix.lower() in (".ts", ".mkv", ".flv"):
        # a piece of a live recording: browsers can't play stream files, so it is repacked once as a normal mp4
        # (no re-encode, takes a moment) and that is what the editor plays
        prev = src.with_name(src.stem + ".preview.mp4")
        if not prev.is_file() or prev.stat().st_mtime < src.stat().st_mtime:
            tmp = prev.with_suffix(".part.mp4")
            try:
                media.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)], timeout=300)
                tmp.replace(prev)
            except Exception:  # noqa: BLE001 - copying failed (odd stream): encode it instead
                media.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-c:a", "aac", "-movflags", "+faststart", str(tmp)], timeout=900)
                tmp.replace(prev)
        src = prev
    return FileResponse(src, media_type="video/mp4", headers={"Cache-Control": "private, max-age=300"})


@app.get("/media/{jid}/{cid}/audio")
def clip_audio(jid: str, cid: str):
    """Lightweight audio-only extract for glitch-free independent audio-layer live preview."""
    job, clip, src = _clip_or_404(jid, cid)
    if not src.is_file():
        raise HTTPException(404, "The original video is no longer kept.")
    a_idx = clip.get("audio_stream_index") if clip.get("audio_stream_index") is not None else job.get("audio_stream_index")
    amap = f"0:a:{int(a_idx)}" if a_idx is not None else "0:a:0"
    aud = src.with_name(f"{src.stem}.audio{f'.s{int(a_idx)}' if a_idx else ''}.m4a")
    if not aud.is_file() or aud.stat().st_mtime < src.stat().st_mtime:
        tmp = aud.with_suffix(".part.m4a")
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", amap, "-vn", "-c:a", "copy",
                       "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)], timeout=120)
            tmp.replace(aud)
        except Exception:  # noqa: BLE001
            media.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", amap, "-vn", "-c:a", "aac",
                       "-b:a", "128k", "-movflags", "+faststart", str(tmp)], timeout=300)
            tmp.replace(aud)
    return FileResponse(aud, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=300"})


@app.get("/media/{jid}/{cid}/strip.jpg")
def clip_strip(jid: str, cid: str, a: float = 0.0, b: float = 0.0, n: int = 12):
    """A row of small frames between two times of the source, for the timeline."""
    job, clip, src = _clip_or_404(jid, cid)
    n = max(4, min(24, int(n)))
    off = clip.get("src_offset", 0.0)
    a, b = max(0.0, float(a) - off), float(b) - off
    if not src.is_file() or not (0 < b - a <= 900):
        raise HTTPException(404)
    out = Path(job["dir"]) / "clips" / f"strip_{cid}_{int(a * 10)}_{int(b * 10)}_{n}.jpg"
    if not out.is_file():
        step = (b - a) / n
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src),
                       "-vf", f"fps=1/{step:.3f},scale=-2:72,tile={n}x1", "-frames:v", "1", "-q:v", "5", str(out)], timeout=300)
        except Exception:  # noqa: BLE001 - the timeline still works without pictures
            pass
    if not out.is_file():
        raise HTTPException(404)
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/jobs/{jid}/clips/{cid}/face")
def clip_face(jid: str, cid: str):
    """Where the speaker's face is in the source, as fractions of the picture, or null when none is found."""
    job, clip, src = _clip_or_404(jid, cid)
    if not src.is_file():
        raise HTTPException(404, "The original video is no longer kept.")
    off = clip.get("src_offset", 0.0)
    try:
        box = faces.find(src, clip["start"] - off, clip["end"] - off)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, safe.friendly(e, 200))
    return {"face": box}


@app.get("/api/jobs/{jid}/clips/{cid}/cambox")
def clip_cambox(jid: str, cid: str):
    """Auto-detect the streamer's static rectangular OBS webcam cutout [x, y, w, h] without face tracking."""
    _, clip, src = _clip_or_404(jid, cid)
    if not src.is_file():
        raise HTTPException(404, "The original video is no longer kept.")
    off = clip.get("src_offset", 0.0)
    try:
        res = faces.find_cam_box(src, max(0.0, clip["start"] - off), max(0.0, clip["end"] - off))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, safe.friendly(e, 200))
    return {"box": res["box"] if res else None, "method": res["method"] if res else None}


@app.get("/api/jobs/{jid}/clips/{cid}/track")
def clip_track(jid: str, cid: str, start: float = None, end: float = None):
    """Where the face goes across the clip (or across the start and end being tried in the editor)."""
    job, clip, src = _clip_or_404(jid, cid)
    if not src.is_file():
        raise HTTPException(404, "The original video is no longer kept.")
    off = clip.get("src_offset", 0.0)
    a, b = (clip["start"] if start is None else float(start)), (clip["end"] if end is None else float(end))
    if not 1 <= b - a <= 200:
        raise HTTPException(400, "Those times aren't valid.")
    try:
        keys = faces.track(src, a - off, b - off)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, safe.friendly(e, 200))
    return {"start": a, "end": b, "keys": keys}


@app.get("/api/jobs/{jid}/clips/{cid}/words")
def clip_words(jid: str, cid: str, start: float, end: float, fresh: int = 0):
    """What is said between start and end, from the video's transcript: the captions for a clip made longer.
    fresh=1 (Regenerate captions): this stretch is heard again by Whisper, which gets Hindi and Hinglish right."""
    job, clip, src = _clip_or_404(jid, cid)
    wf = Path(job["dir"]) / "words.json"
    words = json.loads(wf.read_text(encoding="utf-8")) if wf.is_file() else []
    heard = False
    if fresh:
        from . import hear, roman
        if hear.ready() and src.is_file():
            _throttle("hear:" + ctx.uid(), 30, 600, "That's a lot of regenerating in a short time. Wait a few minutes.")
            off = clip.get("src_offset", 0.0)
            try:
                sel_lang = (job.get("options") or {}).get("lang") or ""
                sel_lang = "" if sel_lang == "auto" else sel_lang
                stream_index = clip.get("audio_stream_index")
                if stream_index is None:
                    stream_index = job.get("audio_stream_index")
                hw, hl = hear.clip_words(src, start - off - 0.5, end - off + 0.5, off,
                                         job.get("title") or "", lang=sel_lang, stream_index=stream_index)
                hw = hear.streamer_only(hear.tidy_mixed(hw, lang=sel_lang or hl), job.get("uploader") or "")     # the streamer's words only
                if hw:
                    words = [w for w in words if w["s"] < start - 0.5] + hw + [w for w in words if w["s"] > end + 0.5]
                    heard = True
                    clip["heard_by"] = "whisper"
                    jobs.save(job)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, "Couldn't hear the clip again right now: " + safe.friendly(e, 120))
    return {"heard": heard, "words": [{"w": str(w.get("w", "")), "s": w["s"], "e": w["e"]} for w in words if start - 40 <= w["s"] <= end + 40]}


@app.post("/api/jobs/{jid}/clips/{cid}/translate-captions")
def clip_translate_captions(jid: str, cid: str, body: dict = Body(...)):
    job, _, _ = _clip_or_404(jid, cid)
    items = body.get("words")
    if not isinstance(items, list) or not 1 <= len(items) <= 3000:
        raise HTTPException(422, "Select a clip with captions to translate.")
    words = []
    try:
        for item in items:
            text = item["w"].strip()[:40]
            start, end = float(item["s"]), float(item["e"])
            if not text or not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError
            words.append({"w": text, "s": start, "e": end})
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(422, "The clip has invalid caption timings.")
    _throttle("caption-translate:" + ctx.uid(), 12, 600)
    usage.set_context(job.get("creator"), job.get("title") or "")
    try:
        return {"words": brain.translate_caption_words(words, job.get("lang") or "mixed speech", job.get("title") or "")}
    except Exception as error:
        reason = str(error).lower()
        if any(key in reason for key in ("quota", "http 429", "per day", "(tpd)", "used up for now")):
            raise HTTPException(429, "The AI provider is rate-limited. Try again after it resets. Your captions were kept.")
        raise HTTPException(502, "English translation is unavailable right now. Your captions were kept.")


@app.post("/api/jobs/{jid}/clips/{cid}/qc")
def clip_qc(jid: str, cid: str, body: dict = Body(...)):
    """Proof-read the captions the editor is showing. Gives back word fixes; the editor applies them."""
    job, clip, _ = _clip_or_404(jid, cid)
    if not config.ai_ready():
        raise HTTPException(400, "Caption check needs an AI key. Add one under Settings, AI.")
    _throttle("qc:" + ctx.uid(), 40, 600, "That's a lot of caption checks in a short time. Wait a few minutes.")
    words = [str(w)[:40] for w in (body.get("words") or [])][:600]
    if not words:
        return {"fixes": []}
    usage.set_context(job.get("creator"), job.get("title") or "")
    try:
        opt_lang = (job.get("options") or {}).get("lang")
        qc_lang = opt_lang if opt_lang not in (None, "", "auto") else (job.get("lang") or "")
        if body.get("language") == "english":
            qc_lang = "en"
        fixes = brain.caption_qc(words, job.get("title") or "", job.get("uploader") or "", lang=qc_lang)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, safe.friendly(e, 200))
    return {"fixes": [{"i": i, "w": w, "was": words[i]} for i, w in fixes]}


@app.get("/api/sfx/search")
def sfx_search(q: str = "meme", page: int = 1):
    """Search meme sounds."""
    ctx.uid()
    _throttle("sfxs:" + ctx.uid(), 60, 600, "That's a lot of searches. Wait a minute.")
    try:
        return {"results": sfx.search(q.strip() or "meme", page)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't search meme sounds right now. Check your internet.")


@app.post("/api/sfx/fetch")
def sfx_fetch(body: dict = Body(...)):
    """Keep a meme sound on this PC, so it can go into clips."""
    ctx.uid()
    _throttle("sfxf:" + ctx.uid(), 120, 600, "That's a lot of sounds. Wait a minute.")
    try:
        f = sfx.fetch(str(body.get("path") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't download that sound. " + safe.friendly(e, 80))
    return {"file": f.name}


@app.get("/api/sfx/file/{name}")
def sfx_file(name: str):
    ctx.uid()
    f = sfx.local(name)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/sfx/pack")
def sfx_pack():
    ctx.uid()
    return {"pack": sfx.library(), "moods": list(sfx.MOODS), "settings": sfx.settings()}


@app.post("/api/jobs/{jid}/clips/{cid}/memes")
def upload_meme(jid: str, cid: str, body: dict = Body(...)):
    import base64
    from io import BytesIO
    from uuid import uuid4
    from PIL import Image, ImageOps

    job, _, _ = _clip_or_404(jid, cid)
    value = body.get("image")
    if not isinstance(value, str):
        raise HTTPException(422, "Choose a PNG, JPEG, or WebP reaction image.")
    _throttle("meme-upload:" + ctx.uid(), 30, 600)
    path = Path(job["dir"]) / "memes"
    path.mkdir(exist_ok=True)
    name = f"meme-{uuid4().hex}.png"
    try:
        with Image.open(BytesIO(base64.b64decode(value, validate=True)), formats=("PNG", "JPEG", "WEBP")) as source:
            if source.width * source.height > 24_000_000:
                raise ValueError
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((1080, 1080), Image.Resampling.LANCZOS)
            image.save(path / name, "PNG")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Couldn’t read that reaction image.")
    return {"file": name}


@app.get("/api/jobs/{jid}/memes/{name}")
def meme_file(jid: str, name: str):
    job = _job_or_404(jid)
    if not re.fullmatch(r"meme-[a-f0-9]{32}\.png", name):
        raise HTTPException(404)
    path = Path(job["dir"]) / "memes" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/sfx/pack")
def sfx_pack_add(body: dict = Body(...)):
    ctx.uid()
    try:
        return {"pack": sfx.pack_add(str(body.get("path") or ""), str(body.get("name") or ""), str(body.get("mood") or "boom"))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't download that sound. " + safe.friendly(e, 80))


@app.delete("/api/sfx/pack/{name}")
def sfx_pack_del(name: str):
    ctx.uid()
    return {"pack": sfx.pack_remove(name)}


@app.put("/api/sfx/settings")
def sfx_settings(body: dict = Body(...)):
    ctx.uid()
    kw = {}
    if "auto" in body:
        kw["auto"] = bool(body["auto"])
    if "db" in body:
        kw["db"] = int(max(-30, min(0, float(body["db"]))))
    if "per_clip" in body:
        kw["per_clip"] = int(max(1, min(5, int(body["per_clip"]))))
    return sfx.save_settings(**kw)


@app.post("/api/jobs/{jid}/clips/{cid}/sfx/auto")
def clip_sfx_auto(jid: str, cid: str, body: dict = Body(default={})):
    """Let the AI place meme sounds on this clip now (the editor shows them; Save makes them real)."""
    job, clip, _ = _clip_or_404(jid, cid)
    if not config.ai_ready():
        raise HTTPException(400, "Meme sounds by AI need an AI key. Add one under Settings, AI.")
    _throttle("sfxa:" + ctx.uid(), 30, 600, "That's a lot of tries. Wait a minute.")
    wf = Path(job["dir"]) / "words.json"
    words = clip.get("fixed_words") or (json.loads(wf.read_text(encoding="utf-8")) if wf.is_file() else [])
    usage.set_context(job.get("creator"), job.get("title") or "")
    only = [str(f) for f in (body or {}).get("files") or []][:30] or None
    return {"sfx": sfx.auto_pick(words, clip["start"], clip["end"], job.get("title") or "", only=only)}


@app.get("/api/fonts")
def font_list():
    """The fonts captions can use, with what the editor needs to draw them at the size the real clip will have."""
    ctx.uid()
    out = []
    for key, f in render.fonts().items():
        m = render._Font.get(key)
        out.append({"key": key, "label": f["label"], "em": round(m.upm / (m.asc + m.desc), 4)})
    styles = {k: {"size": v["size"], "font": next((key for key, f in render.fonts().items() if f["path"] == str(render._font_path(v["font"]))), ""),
                  "upper": bool(v["upper"])} for k, v in render.STYLES.items()}
    hook_key = next((key for key, f in render.fonts().items() if f["path"] == str(render._font_path(render.HOOK["font"]))), "")
    return {"fonts": out, "styles": styles, "hook": {"size": render.HOOK["size"], "font": hook_key}}


@app.get("/api/fonts/{key}")
def font_file(key: str):
    ctx.uid()
    f = render.fonts().get(key)
    if not f:
        raise HTTPException(404)
    return FileResponse(f["path"], media_type="font/otf" if f["path"].lower().endswith(".otf") else "font/ttf",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/jobs/{jid}/ask")
def ask_for_clip(jid: str, body: dict = Body(...)):
    """'Clip the part where he says...': the AI finds that moment in the transcript and the clip is made."""
    from . import askclip
    job = _job_or_404(jid)
    if not config.ai_ready():
        raise HTTPException(400, "This needs the AI. Add a key under Settings, AI.")
    _throttle("ask:" + ctx.uid(), 20, 600, "That's a lot of requests in a short time. Wait a few minutes.")
    ask = " ".join(str(body.get("text", "")).split())[:300]
    s = live.mine(jid) if job.get("kind") == "live" else None
    if job.get("kind") == "live" and (not s or s.ended):
        raise HTTPException(400, "This recording has ended, so its stream isn't kept any more. Ask while it's recording.")
    if s:
        words = list(s.words)
    else:
        wf = Path(job["dir"]) / "words.json"
        words = json.loads(wf.read_text(encoding="utf-8")) if wf.is_file() else []
    if job.get("status") in ("queued",) or (not s and not words):
        raise HTTPException(400, "This video is still being read. Ask again once it has its first clips.")
    o = job["options"]
    usage.set_context(job.get("creator"), job.get("title") or "")
    try:
        got = askclip.find(words, ask, job.get("title") or "", int(o.get("min_len", 15)), int(o.get("max_len", 60)),
                           loud=(s.db if s else None), n=int(body.get("n") or 2))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "The AI couldn't look right now: " + safe.friendly(e, 140))
    if not got["clips"]:
        return {"made": [], "say": got["say"] or "Couldn't find that moment. Try describing it differently."}
    if s:
        for c in got["clips"]:
            s.manual.append({"start": c["start"], "end": min(c["end"], s.t_done), "why": f"You asked: {ask[:100]}"})
        made = [c.get("title") or "clip" for c in got["clips"]]
    else:
        jobs.add_asked(jid, got["clips"], ask)
        made = [c.get("title") or "clip" for c in got["clips"]]
    return {"made": made, "say": got["say"], "clips": got["clips"]}


@app.post("/api/jobs/{jid}/more")
def more_clips(jid: str):
    job = _job_or_404(jid)
    _throttle("more:" + ctx.uid(), 12, 600, "That's a lot of new searches in a short time. Wait a few minutes.")
    try:
        jobs.more(jid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return jobs.public(job)


@app.post("/api/jobs/{jid}/clips/{cid}/score")
def clip_score(jid: str, cid: str):
    """Refresh the same finished-clip score shown on the card."""
    from . import scores

    job, clip, _src = _clip_or_404(jid, cid)
    if not config.ai_ready():
        raise HTTPException(400, "This needs an AI key. Add one under Settings, AI.")
    if scores.checking(job, clip):
        raise HTTPException(409, "This clip is already being scored. Its card will update automatically.")
    _throttle("score:" + ctx.uid(), 30, 600, "That's a lot of checks in a short time. Wait a few minutes.")
    try:
        return {"perf": scores.review(job, clip)}
    except scores.NoSpeech as e:
        raise HTTPException(400, str(e))
    except scores.Changed as e:
        raise HTTPException(409, str(e))
    except Exception:
        raise HTTPException(502, "The AI could not score this clip right now.")


def _yt_file():
    return config.DATA_DIR / "youtube_cookies.txt"


@app.get("/api/youtube")
def youtube_state():
    """Whether YouTube downloads go through a saved sign-in, and since when. Never the sign-in itself."""
    ctx.uid()
    f = _yt_file()
    on = f.is_file() and f.stat().st_size > 0
    return {"connected": on, "since": f.stat().st_mtime if on else None, "blocked": media.youtube_paused()}


@app.post("/api/youtube/cookies")
async def youtube_cookies(file: UploadFile = File(...)):
    """A cookies.txt exported from a browser, for when the sign-in window can't be used."""
    ctx.uid()
    raw = (await file.read(2_000_000)).decode("utf-8", "ignore")
    lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#") and l.count("\t") >= 6]
    yt = [l for l in lines if "youtube.com" in l.split("\t")[0]]
    if not yt:
        raise HTTPException(400, "That file has no YouTube sign-in in it. Export it while you are on youtube.com and signed in.")
    _yt_file().write_text("# Netscape HTTP Cookie File\n" + "\n".join(yt) + "\n", encoding="utf-8")
    media.youtube_unpause()
    return {"connected": True}


@app.delete("/api/youtube")
def youtube_disconnect():
    ctx.uid()
    _yt_file().unlink(missing_ok=True)
    return {"connected": False}


@app.post("/api/youtube/retry")
def youtube_retry():
    """After connecting: let YouTube links be tried again at once."""
    ctx.uid()
    media.youtube_unpause()
    return {"ok": True}


UPDATE_OK = (".py", ".html", ".css", ".js", ".json", ".txt", ".png", ".svg", ".ico", ".woff2", ".ttf", ".otf", ".webp", ".jpg")


def _version():
    f = config.ROOT / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.is_file() else "unknown"


def _vid_len(f) -> float:
    """How long a video is, sound or no sound. Raises if it isn't a video."""
    out = media.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type:format=duration",
                     "-of", "csv=p=0", str(f)])
    if "video" not in out:
        raise RuntimeError("no picture in it")
    return float([l for l in out.split() if l.replace(".", "", 1).isdigit()][-1])


@app.get("/api/outro")
def outro_state():
    ctx.uid()
    f, on = config.outro()
    if not f:
        return {"has": False, "on": on}
    try:
        secs = _vid_len(f)
    except Exception:  # noqa: BLE001
        secs = 0
    return {"has": True, "on": on, "seconds": round(secs, 1), "name": f.name}


@app.get("/api/outro/file")
def outro_file():
    ctx.uid()
    f, _ = config.outro()
    if not f:
        raise HTTPException(404)
    return FileResponse(f)


@app.post("/api/outro")
async def outro_upload(file: UploadFile = File(...)):
    """A short video added to the end of every clip."""
    ctx.uid()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise HTTPException(400, "Pick a video file (mp4, mov or webm).")
    d = config.DATA_DIR / "brand"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"incoming{ext}"
    tmp.write_bytes(await file.read(60_000_000))
    try:
        secs = _vid_len(tmp)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "That video can't be read: " + safe.friendly(e, 120))
    if secs > 15.5:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"That outro is {secs:.0f} seconds. Keep it to 15 seconds or less (3-5 works best).")
    for old in d.glob("outro.*"):
        if old.suffix != ".json":
            old.unlink(missing_ok=True)
    tmp.rename(d / f"outro{ext}")
    (d / "outro.json").write_text(json.dumps({"on": True}), encoding="utf-8")
    return outro_state()


@app.put("/api/outro")
def outro_switch(body: dict = Body(...)):
    ctx.uid()
    d = config.DATA_DIR / "brand"
    d.mkdir(parents=True, exist_ok=True)
    (d / "outro.json").write_text(json.dumps({"on": bool(body.get("on"))}), encoding="utf-8")
    return outro_state()


@app.delete("/api/outro")
def outro_delete():
    ctx.uid()
    for f in (config.DATA_DIR / "brand").glob("outro.*"):
        f.unlink(missing_ok=True)
    return {"has": False, "on": True}


@app.get("/api/update")
def update_state():
    ctx.uid()
    return {"version": _version()}


@app.post("/api/update")
async def update_install(file: UploadFile = File(...)):
    """Install a Upfern-Update.zip by hand (Settings, Updates). Videos, settings and keys are never touched."""
    ctx.uid()
    try:
        n = updater.install_zip(await file.read(60_000_000))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"installed": n, "version": _version()}


@app.get("/api/update/check")
def update_check(force: int = 0):
    return updater.check(force=bool(force))


@app.post("/api/update/auto")
def update_auto():
    """Download the newest version from the owner's site, install it, and restart Upfern."""
    _throttle("autoupd:" + ctx.uid(), 5, 600, "Give it a minute.")
    try:
        out = updater.download_and_install()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't download the update: " + safe.friendly(e, 120))
    updater.restart_soon()
    return out


@app.get("/api/gameplay")
def gameplay_list():
    ctx.uid()          # signed-in people only
    return {"files": render.gameplay_files()}


@app.get("/api/gameplay/{name}")
def gameplay_file(name: str):
    ctx.uid()          # signed-in people only
    if name not in render.gameplay_files():
        raise HTTPException(404)
    return FileResponse(render.GAMEPLAY_DIR / name, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/jobs/{jid}/clips/{cid}/caption")
def clip_caption(jid: str, cid: str, body: dict = Body(...)):
    """Post text for a clip. {"write": true} has it written (by the AI when one is set up); otherwise the title,
    description and hashtags sent are saved as they are. Nothing is rendered again."""
    job, clip, _ = _clip_or_404(jid, cid)
    by = "you"
    if body.get("write"):
        _throttle("caption:" + ctx.uid(), 30, 600, "That's a lot of captions in a short time. Wait a few minutes.")
        wf = Path(job["dir"]) / "words.json"
        words = clip.get("fixed_words") or (json.loads(wf.read_text(encoding="utf-8")) if wf.is_file() else [])
        text = " ".join(str(w.get("w", "")) for w in words if clip["start"] - 0.2 <= w["s"] <= clip["end"])
        usage.set_context(job.get("creator"), job.get("title") or "")
        cap, by = brain.write_caption(text, job.get("title") or "", job.get("uploader") or "")
        if not (cap["title"] or cap["hook"]):
            raise HTTPException(400, "Nothing is said in this clip, so there is nothing to write a caption from.")
        clip.update(title=cap["title"][:100], description=cap["description"][:2000], hashtags=cap["hashtags"][:12])
        if cap.get("highlight_words"):
            clip["highlight_words"] = cap["highlight_words"][:6]
        hook = cap["hook"][:60]
    else:
        tags = body.get("hashtags")
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[\s,#]+", tags) if t]
        clip.update(title=str(body.get("title") or "")[:100], description=str(body.get("description") or "")[:2000],
                    hashtags=[re.sub(r"[^\w]", "", str(t))[:40] for t in (tags or [])][:12])
        hook = clip.get("hook") or ""
    jobs.save(job)
    ctx.bump()
    return {"title": clip.get("title", ""), "description": clip.get("description", ""), "hashtags": clip.get("hashtags", []), "hook": hook, "by": by}


@app.post("/api/jobs/{jid}/clips/{cid}/split")
def clip_split(jid: str, cid: str, body: dict = Body(...)):
    job, clip, _ = _clip_or_404(jid, cid)
    if job["status"] in ("queued", "running") or clip.get("status") in ("queued", "rendering"):
        raise HTTPException(409, "This clip is still being made. Try again when it has finished.")
    _throttle("render:" + ctx.uid(), 20, 600, "That's a lot of re-makes in a short time. Wait a few minutes.")
    try:
        new = jobs.split_clip(jid, cid, float(body.get("at")))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"new": new, "job": jobs.public(job)}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] not in ("queued", "running"):
        raise HTTPException(409, "This video is not being worked on any more.")
    jobs.cancel(jid)
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    job = _job_or_404(jid)
    if job["status"] in ("queued", "running", "live") or jid in live.SESSIONS:
        raise HTTPException(409, "This is still being worked on. Stop it or wait for it to finish first.")
    jobs.delete(jid)
    return {"ok": True}


def _pairs(v, limit=300) -> list:
    """[[video id, clip id], ...] from the page, checked."""
    out = []
    for x in (v if isinstance(v, list) else [])[:limit]:
        if isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(y, str) and re.fullmatch(r"[\w\-]{1,60}", y) for y in x):
            out.append((x[0], x[1]))
    return out


@app.delete("/api/jobs/{jid}/clips/{cid}")
def delete_clip(jid: str, cid: str):
    _job_or_404(jid)
    try:
        if not account.delete_clip(jid, cid):
            raise HTTPException(404, "No such clip")
    except account.Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/bulk/delete")
def bulk_delete(body: dict = Body(...)):
    _throttle("bulkdel:" + ctx.uid(), 30, 600)
    vids = [x for x in (body.get("videos") if isinstance(body.get("videos"), list) else [])[:300]
            if isinstance(x, str) and re.fullmatch(r"[\w\-]{8,60}", x)]
    return account.delete_many(vids, _pairs(body.get("clips")))


@app.get("/api/clips.zip")
def clips_zip(ids: str = ""):
    """Many clips as one download. `ids` is video.clip pairs separated by commas."""
    _throttle("zip:" + ctx.uid(), 12, 600, "Give it a moment: you just downloaded several groups of clips.")
    pairs = _pairs([p.split(".", 1) for p in ids.split(",") if "." in p][:300])
    try:
        f = account.zip_clips(pairs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from starlette.background import BackgroundTask
    return FileResponse(f, filename=f"peakcut-clips-{len(pairs)}.zip", media_type="application/zip",
                        headers={"Cache-Control": "private, no-store"}, background=BackgroundTask(lambda: f.unlink(missing_ok=True)))


@app.delete("/api/creators/{cid}")
def delete_creator(cid: str):
    _throttle("cdel:" + ctx.uid(), 30, 600)
    try:
        r = account.delete_creator(cid)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    if not r.get("ok"):
        raise HTTPException(404, "No such creator")
    return r


@app.post("/api/account/delete")
def delete_my_account(request: Request, body: dict = Body(...)):
    """Deletes the signed-in account and everything in it. The page asks for the email to be typed, so it cannot
    happen by a stray click or a request the person did not mean to make."""
    me_ = _me(request)
    if str(body.get("confirm") or "").strip().lower() != me_.email.lower():
        raise HTTPException(400, "Type your email address exactly to confirm.")
    try:
        account.delete_account(me_.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.cookie_args("x")["key"], path="/")
    return resp


@app.get("/media/{jid}/{name}")
def media_file(jid: str, name: str, download: int = 0):
    if not re.fullmatch(r"[\w\-]+\.(mp4|jpg)", name):
        raise HTTPException(404)
    f = Path(_job_or_404(jid)["dir"]) / "clips" / name
    if not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, filename=name if download else None, headers={"Cache-Control": "private, no-cache"})


# ================================================================ creators and live recording
def _channel_ok(body: dict):
    links = [str(body.get("channel") or "").strip()]
    chs = body.get("channels")
    if chs is not None and not isinstance(chs, dict):
        raise HTTPException(400, "Bad channel list.")
    for plat, url in (chs or {}).items():
        url = str(url or "").strip()
        if url and creators.platform_key(url) != plat:
            raise HTTPException(400, f"That link isn't a {plat.title()} channel.")
        links.append(url)
    for ch in links:
        if ch and not media.site_allowed(live.normalize(ch)):
            raise HTTPException(400, "Channel links must be from YouTube, Twitch or Kick.")


@app.get("/api/admin/email-preview")
def email_preview(request: Request, kind: str = "verify"):
    """What account emails look like, for admins."""
    from . import mailer
    _admin(request)
    if kind == "reset":
        page = mailer.preview("Reset your password", "Someone asked to reset the password for this Upfern account. If that was you, choose a new one below.",
                              "Choose a new password", config.PUBLIC_URL + "/reset?t=example",
                              "The link works for one hour. If you didn't ask for this, ignore this email and your password stays the same.")
    else:
        page = mailer.preview()
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/test-email")
def test_email(request: Request):
    """Send the confirm-email template to the admin, to check the email service works."""
    from . import mailer
    me_ = _admin(request)
    if not mailer.ready():
        raise HTTPException(400, "No email service is set up yet. Add the PEAKCUT_SMTP settings to .env first.")
    mailer.send(me_.email, "Upfern test email", "Your emails are working",
                "This is a test from your Upfern server. Account emails like this one will now reach people.",
                "Open Upfern", config.PUBLIC_URL + "/app", "You can ignore this message.")
    return {"sent_to": me_.email}


@app.post("/api/feedback")
def send_feedback(request: Request, body: dict = Body(...)):
    from . import feedback
    u = _me(request)
    try:
        auth.throttle("feedback:" + u.id, 10, 3600)
        return feedback.submit(u.id, str(body.get("kind", "")), str(body.get("message", "")),
                               str(body.get("page", "")), str(body.get("context", "")))
    except auth.AuthError:
        raise HTTPException(429, "You've sent a lot of messages in the last hour. Try again a bit later.")
    except feedback.Refused as e:
        raise HTTPException(400, str(e))


@app.get("/api/admin/feedback")
def admin_feedback(request: Request, status: str = ""):
    from . import feedback
    _admin(request)
    return feedback.listing(status)


@app.post("/api/admin/feedback/{fid}")
def admin_feedback_status(request: Request, fid: int, body: dict = Body(...)):
    from . import feedback
    _admin(request)
    try:
        feedback.set_status(fid, str(body.get("status", "")))
    except feedback.Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/notifications")
def notifications_list():
    from . import notices
    return notices.mine()


@app.post("/api/notifications/read")
def notifications_read():
    from . import notices
    notices.read_all()
    return {"ok": True}


@app.get("/api/me/photo")
def my_photo(request: Request):
    u = _me(request)
    got = auth.photo(u.id) if u else None
    if not got:
        raise HTTPException(404)
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/channel-preview")
def channel_preview(request: Request, url: str = ""):
    url = url.strip()[:300]
    if not url or not media.site_allowed(live.normalize(url)):
        raise HTTPException(400, "Paste a YouTube, Twitch or Kick channel link.")
    try:
        auth.throttle(f"chanprev:{_me(request).id}", 12, 60)
    except auth.AuthError:
        raise HTTPException(429, "Too many lookups at once. Wait a moment.")
    try:
        return _lookup(profiles.preview, url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Couldn't open that channel. Check the link.")


@app.get("/api/creators/{cid}/avatar")
def creator_avatar(cid: str, p: str = ""):
    c = creators.get(cid)
    if not c:
        raise HTTPException(404)
    key = p if p in creators.PLATFORMS else creators.platform_key(c["channel"])
    prof = (c.get("profiles") or {}).get(key) or {}
    f = creators.root(cid) / f"avatar-{key}.img"
    if not f.is_file() or prof.get("pic") not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(404)
    return FileResponse(f, media_type=prof["pic"], headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/creators")
def list_creators():
    return _creator_rows()


@app.post("/api/creators")
def new_creator(body: dict = Body(...)):
    _channel_ok(body)
    if len(creators.all_creators()) >= 50:
        raise HTTPException(400, "You've reached the limit of 50 creators.")
    try:
        c = creators.create(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if c.get("channels"):
        profiles.refresh(c["id"], force=True)
    return c


@app.put("/api/creators/{cid}")
def edit_creator(cid: str, body: dict = Body(...)):
    _throttle("cedit:" + ctx.uid(), 60, 600)
    _channel_ok(body)
    try:
        c = creators.update(cid, body)
    except KeyError:
        raise HTTPException(404, "No such creator")
    except ValueError as e:
        raise HTTPException(400, str(e))
    s = live.active_for(cid)
    if s:
        s.settings = c  # new settings apply to the running recording straight away
    if "channels" in body or "channel" in body:
        profiles.refresh(cid, force=True)
    if body.get("auto_watch") and c.get("auto_watch") and not s:
        live.check_now(cid)    # if they are live right now, recording starts within seconds
    return c


@app.post("/api/creators/{cid}/live")
def go_live(cid: str, body: dict = Body(default={})):
    _need_verified()
    try:
        c = creators.get(cid)
    except ValueError:
        c = None
    if not c:
        raise HTTPException(404, "No such creator")
    # the stream the user is watching, else where they are live right now, else the channel chosen in Setup
    on_air = [p["channel"] for k, p in (c.get("profiles") or {}).items() if p.get("live") and c["channels"].get(k) == p.get("channel")]
    asked = str((body or {}).get("channel") or "")
    url = asked if asked in c["channels"].values() else c["channel"] if c["channel"] in on_air or not on_air else on_air[0]
    try:
        return jobs.public(live.start_session(c, url))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/live/quick")
def quick_live(body: dict = Body(...)):
    """Paste a live link, start clipping it: the stream's creator workspace is reused, or made on the spot."""
    _need_verified()
    _throttle("quicklive:" + ctx.uid(), 10, 600)
    url = media.normalize(str(body.get("url", ""))[:500])
    if not media.site_allowed(url):
        raise HTTPException(400, "Paste a YouTube, Twitch or Kick live link.")
    try:
        info = media.preview(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, media.explain(e))
    if not info.get("is_live"):
        raise HTTPException(400, "That video isn't live right now. Press Make clips to clip it as a normal video.")
    name = (info.get("uploader") or "Live stream").strip()[:60]
    c = next((x for x in creators.all_creators() if x.get("channel") == url or x["name"].lower() == name.lower()), None)
    if c and c.get("channel") != url:
        chans = {**(c.get("channels") or {}), creators.platform_key(url): url}
        c = creators.update(c["id"], {"channels": chans, "channel": url})
    if not c:
        c = creators.create({"name": name, "channel": url})
    running = live.active_for(c["id"])
    if running:
        return {"job": running.job["id"], "creator": c["id"], "already": True}
    try:
        opts = body.get("options") if isinstance(body.get("options"), dict) else {}
        job = live.start_session(c, url, options=opts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job": job["id"], "creator": c["id"]}


@app.get("/api/ai/backup")
def ai_backup_get():
    ctx.uid()
    b = ai.backup()
    return {"need": brain.need_backup(), "has": bool(b), "models": b.get("models", []), "key_end": (b.get("key") or "")[-4:]}


@app.post("/api/ai/backup")
def ai_backup_set(body: dict = Body(...)):
    """A free Gemini key, used only when the main AI runs out for the day."""
    ctx.uid()
    _throttle("aibk:" + ctx.uid(), 10, 600)
    try:
        r = ai.save_backup(body.get("key", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't check the key with Google right now: " + safe.friendly(e, 100))
    brain._NEED["at"] = 0.0
    return {"ok": True, **r}


@app.delete("/api/ai/backup")
def ai_backup_del():
    ctx.uid()
    ai._backup_file().unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/purge")
def purge_now():
    """The Purge button: free memory and clear temporary files (no clips, videos or settings are touched)."""
    from . import purge
    ctx.uid()
    _throttle("purge:" + ctx.uid(), 10, 600)
    return purge.run()


@app.get("/api/cover/providers")
def cover_providers(request: Request):
    from . import cover_ai, cover_cutout
    if config.ACCOUNTS:
        _admin(request)
    return {**{p: bool(cover_ai.provider_key(p)) for p in cover_ai.MODELS}, "withoutbg": bool(cover_cutout.cloud_key())}


@app.put("/api/cover/providers/{provider}")
def cover_connect(provider: str, request: Request, body: dict = Body(...)):
    import httpx
    from . import cover_ai, cover_cutout
    if config.ACCOUNTS:
        _admin(request)
    if provider not in cover_ai.MODELS and provider != "withoutbg":
        raise HTTPException(404)
    key = str(body.get("key") or "").strip()
    if not 16 <= len(key) <= 400 or any(c.isspace() for c in key):
        raise HTTPException(422, "Enter an API key from the provider’s developer account.")
    _throttle("cover-connect:" + ctx.uid(), 12, 600)
    try:
        if provider == "withoutbg":
            cover_cutout.check_cloud_key(key)
        else:
            cover_ai.check_key(provider, key)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (400, 401, 403, 404):
            raise HTTPException(400, "The key could not access the image model. Check the key and its project permissions.")
        raise HTTPException(502, "The image provider could not verify the key right now.")
    except httpx.RequestError:
        raise HTTPException(502, "Could not reach the image provider.")
    ai.remember_key("cover_" + provider, key)
    return {"ok": True}


@app.post("/api/cover/cutout")
def cover_remove_background(request: Request, body: dict = Body(...)):
    import cv2
    import httpx
    from PIL import Image
    from . import cover_ai, cover_cutout
    if config.ACCOUNTS:
        _admin(request)
    source, provider = body.get("image"), body.get("provider", "local")
    if not isinstance(source, str) or provider not in ("local", "withoutbg"):
        raise HTTPException(422, "Choose a frame and a cutout engine.")
    try:
        frame = cover_ai.image_bytes(source)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "Choose a valid PNG, JPEG, or WebP image.")
    key = cover_cutout.cloud_key() if provider == "withoutbg" else ""
    if provider == "withoutbg" and not key:
        raise HTTPException(409, "Connect withoutBG or use the local remover.")
    _throttle("cover-cutout:" + ctx.uid(), 30, 600)
    try:
        image = cover_cutout.cloud(frame,key) if key else cover_cutout.local(frame)
    except httpx.HTTPStatusError as error:
        if key and error.response.status_code in (401, 402, 403, 429):
            raise HTTPException(409, "withoutBG could not use this key or has reached its limit. The local remover is available.")
        raise HTTPException(502, "The background remover is unavailable right now.")
    except (cv2.error, httpx.RequestError, OSError, ValueError):
        raise HTTPException(502, "Couldn’t isolate a subject in this image. Try a tighter or clearer frame.")
    return Response(image, media_type="image/png", headers={"Cache-Control":"no-store"})


@app.post("/api/cover/generate")
def cover_generate(request: Request, body: dict = Body(...)):
    import httpx
    from PIL import Image
    from . import cover_ai
    if config.ACCOUNTS:
        _admin(request)
    provider = body.get("provider")
    if not isinstance(provider, str) or provider not in cover_ai.MODELS:
        raise HTTPException(422, "Choose Gemini or ChatGPT.")
    key = cover_ai.provider_key(provider)
    if not key:
        raise HTTPException(409, "Connect this image provider first.")
    prompt = str(body.get("prompt") or "").strip()
    source = body.get("image")
    if not prompt or len(prompt) > 4000 or not isinstance(source, str):
        raise HTTPException(422, "Choose a reference image and enter a prompt.")
    try:
        frame = cover_ai.image_bytes(source)
    except (ValueError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, "Choose a valid PNG, JPEG, or WebP image.")
    _throttle("cover-generate:" + ctx.uid(), 8, 600, "Too many cover generations. Try again in a few minutes.")
    try:
        image = cover_ai.generate(provider, key, frame, prompt)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code == 429:
            raise HTTPException(429, "The provider has reached its quota or rate limit. Check the API project’s billing or try again later.")
        if code in (401, 402, 403, 404):
            raise HTTPException(409, "This API project could not use the image model. Check the key, billing, and model access.")
        if code == 400:
            raise HTTPException(422, "The provider could not edit this image with the given prompt. Try a different frame or simplify the prompt.")
        raise HTTPException(502, "The image provider is unavailable right now.")
    except (httpx.RequestError, ValueError, OSError, Image.DecompressionBombError):
        raise HTTPException(502, "No usable image was returned. Your original cover is still available.")
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/ai/pool")
def ai_pool_get():
    """The free AI keys saved on this PC (never the keys themselves), and where to get more."""
    ctx.uid()
    ks = ai.keys()
    main = ai.provider()
    out = []
    for p, (name, link, how) in ai.FREE_KEYS.items():
        k = ks.get(p) or (ai.current()["key"] if main == p else "")
        spent = [m for m in (ai.pool_models(p, k) if k else []) if brain._SPENT.get(brain.spent_tag(p, k) + m, 0) > __import__("time").time()]
        out.append({"id": p, "name": name, "link": link, "how": how, "has": bool(k), "end": k[-4:] if k else "",
                    "main": main == p, "spent": len(spent), "models": len(ai.pool_models(p, k)) if k else 0})
    return {"services": out, "need": brain.need_backup()}


@app.post("/api/ai/pool")
def ai_pool_add(body: dict = Body(...)):
    """Add (or replace) one free key. It is checked first, then used whenever the AI before it runs out."""
    ctx.uid()
    _throttle("pool:" + ctx.uid(), 20, 600)
    p, k = str(body.get("id", "")), str(body.get("key", "")).strip()
    try:
        ms = ai.check_key(p, k)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't check the key right now: " + safe.friendly(e, 100))
    ai.remember_key(p, k)
    brain._NEED["at"] = 0.0
    return {"ok": True, "models": ms}


@app.delete("/api/ai/pool/{pid}")
def ai_pool_del(pid: str):
    ctx.uid()
    ks = ai.keys()
    ks.pop(pid, None)
    ai._keys_file().write_text(__import__("json").dumps(ks), encoding="utf-8")
    return {"ok": True}


@app.get("/api/ai/captions-key")
def captions_key_get():
    ctx.uid()
    return {"has": bool(ai.groq_key())}


@app.post("/api/ai/captions-key")
def captions_key_set(body: dict = Body(...)):
    """A free Groq key, used for Whisper captions (Hindi, Hinglish and English) whatever the main AI is."""
    import httpx
    ctx.uid()
    _throttle("capk:" + ctx.uid(), 10, 600)
    k = str(body.get("key", "")).strip()
    if not k.startswith("gsk_") or len(k) < 30:
        raise HTTPException(400, "That doesn't look like a Groq key. It starts with gsk_.")
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer " + k}, timeout=15)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't reach Groq right now: " + safe.friendly(e, 80))
    if r.status_code in (401, 403):
        raise HTTPException(400, "Groq says that key isn't valid. Copy it again from console.groq.com/keys.")
    ai.remember_key("groq", k)
    return {"ok": True}


@app.get("/api/emoji/apple")
def apple_emoji(e: str):
    """The iPhone-style picture of one emoji, so the editor preview shows what the finished clip will have."""
    from fastapi.responses import FileResponse
    f = render._apple_emoji(str(e)[:12])
    if not f:
        raise HTTPException(404, "No picture for that emoji")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "max-age=604800"})


@app.get("/api/focus")
def get_focus():
    from . import focus
    return {"text": focus.get(), "presets": focus.PRESETS}


@app.post("/api/focus")
def set_focus(body: dict = Body(...)):
    """The instruction for the AI, used by every video and recording started from now on."""
    from . import focus
    return {"text": focus.put(body.get("text", ""))}


@app.post("/api/live/{jid}/focus")
def live_focus(jid: str, body: dict = Body(...)):
    """Change what the AI looks for in a stream that is recording right now."""
    s = live.mine(jid)
    if not s or s.ended:
        raise HTTPException(404, "That stream isn't recording.")
    from . import focus
    s.job["focus"] = focus.understand(body.get("text", ""))
    jobs.save(s.job)
    return {"text": s.job["focus"]}


@app.post("/api/live/{jid}/catchup")
def live_catchup(jid: str):
    """Clip the part of the stream that happened before recording started."""
    try:
        cj = live.catch_up_now(jid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "Couldn't reach the earlier part of the stream: " + str(e)[-120:])
    return {"job": cj["id"], "minutes": round(cj["source"]["past"] / 60)}


@app.post("/api/live/{jid}/review")
def live_review(jid: str):
    """The AI reads everything since its last review right now and makes the best clips."""
    s = live.mine(jid)
    if not s or s.ended:
        raise HTTPException(404, "This recording has already ended")
    _throttle("review:" + ctx.uid(), 6, 600, "The AI is already on it. Give it a minute.")
    s.review_now()
    return {"ok": True}


@app.post("/api/live/{jid}/stop")
def stop_live(jid: str):
    s = live.mine(jid)
    if not s:
        raise HTTPException(404, "This recording has already ended")
    s.stop()
    return {"ok": True}


@app.post("/api/live/{jid}/clip")
def clip_now(jid: str, body: dict = Body(...)):
    s = live.mine(jid)
    if not s or s.ended:
        raise HTTPException(404, "This recording has already ended")
    _throttle("clipnow:" + ctx.uid(), 6, 60, "Give it a moment: your last clips are still being made.")
    if len(s.manual) >= 3:
        raise HTTPException(429, "Give it a moment: your last clips are still being made.")
    try:
        s.clip_last(max(5, min(180, float(body.get("seconds", 30)))))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ================================================================ AI usage
@app.get("/api/usage")
def ai_usage():
    return usage.summary({c["id"]: c["name"] for c in creators.all_creators()})


# ---------------------------------------------------------------- the AI key and model, set from the dashboard
# Only the owner (or an admin, when accounts are on) reaches these. The key is never sent back to the page.
def _ai_error(e) -> str:
    return safe.friendly(e, 300).replace(ai.key() or "\0", "<key>")


@app.get("/api/ai")
def ai_get(request: Request):
    _admin(request)
    return ai.public()


@app.put("/api/ai")
def ai_put(request: Request, body: dict = Body(...)):
    _admin(request)
    k = body.get("key")
    try:
        out = ai.save(body.get("provider"), body.get("models"), body.get("base_url") or "",
                      None if k is None or (isinstance(k, str) and not k.strip() and not body.get("remove_key")) else str(k))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    usage.forget_all()
    ctx.bump()
    return out


@app.delete("/api/ai/keys/{provider}")
def ai_key_remove(request: Request, provider: str, hint: str = ""):
    _admin(request)
    try:
        ai.remove_key(provider, hint)
    except ValueError as e:
        raise HTTPException(409, str(e))
    brain._NEED["at"] = 0.0
    ctx.bump()
    return ai.public()


@app.post("/api/ai/models")
def ai_models(request: Request, body: dict = Body(default={})):
    _admin(request)
    _throttle("aimodels:" + ctx.uid(), 30, 600)
    try:
        return {"models": ai.list_models(body.get("provider"), body.get("base_url"), body.get("key"))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "The service did not give its list of models. Check the key, or type the model yourself. " + _ai_error(e)[:160])


@app.post("/api/ai/test")
def ai_test(request: Request):
    _admin(request)
    _throttle("aitest:" + ctx.uid(), 20, 600)
    try:
        return ai.test()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, "The AI did not answer: " + _ai_error(e))


# ================================================================ posting, accounts, settings
@app.get("/api/overview")
def overview():
    return posting.overview()


@app.get("/api/settings")
def get_settings():
    return accounts.public_settings()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    try:
        accounts.save_settings(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return accounts.public_settings()


@app.get("/api/accounts")
def list_accounts():
    return accounts.list_accounts()


@app.delete("/api/accounts/{aid}")
def remove_account(aid: str):
    accounts.remove(aid)
    return {"ok": True}


# ---------------------------------------------------------------- connecting YouTube, Instagram and TikTok
# Connect sends the browser to the platform's own sign-in page; the platform sends it back here, and the person lands
# on Settings, Connected apps, with the account added (or a message saying what went wrong).
def _back(**q):
    """The sign-in window tells the page that opened it how it went, and closes. Opened as a full page instead,
    it goes back to Settings."""
    from urllib.parse import urlencode as _ue
    from fastapi.responses import HTMLResponse as _H
    return _H("<!doctype html><meta charset=utf-8><title>Upfern</title><body style='background:#0B0D10;color:#F2F4FF;font:15px system-ui;"
              "display:grid;place-items:center;height:100vh;margin:0'><p>Done. You can close this window.</p><script>"
              f"const m={json.dumps(q)};"
              "try{if(window.opener){window.opener.postMessage({clipkar:'connect',...m},location.origin);window.close()}}catch(e){}"
              f"if(!window.opener)location.replace('/app/settings/apps?{_ue(q)}');</script>")


@app.get("/oauth/{platform}/start")
def connect_start(platform: str):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    try:
        return RedirectResponse(accounts.connect_url(platform), 303)
    except ValueError as e:
        return _back(connect_error=str(e))


@app.get("/oauth/{platform}/callback")
def connect_callback(platform: str, code: str = "", state: str = "", error: str = "", error_description: str = "",
                     error_reason: str = ""):
    if platform not in accounts.PLATFORM_NAMES:
        raise HTTPException(404)
    name = accounts.PLATFORM_NAMES[platform]
    if error or not code:
        why = "you cancelled" if error in ("access_denied",) or error_reason == "user_denied" else (error_description or error or "no sign-in code came back")
        return _back(connect_error=f"{name} was not connected: {why[:160]}.")
    try:
        _throttle("connect:" + ctx.uid(), 30, 600)
        acc = accounts.connect_finish(platform, code, state)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return _back(connect_error=f"{name} was not connected: {safe.friendly(e, 240)}")
    return _back(connected=platform, name=acc["name"])


@app.get("/pub/{token}.mp4")
def public_clip(token: str):
    """A clip for a platform to fetch while posting it: one clip, one hour, signed (see accounts.clip_link)."""
    got = accounts.read_clip_link(token)
    if not got:
        raise HTTPException(404)
    uid, job_id, name = got
    with ctx.as_user(uid):
        job = jobs.get(job_id) if re.fullmatch(r"[\w\-]{8,60}", job_id) else None
        f = Path(job["dir"]) / "clips" / name if job else None
    if not f or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@app.get("/api/apps")
def apps_get(request: Request):
    _admin(request)
    return accounts.apps_public()


@app.put("/api/apps")
def apps_put(request: Request, body: dict = Body(...)):
    _admin(request)
    try:
        return accounts.save_app(str(body.get("platform", "")), body.get("client_id"), body.get("client_secret"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/posts")
def add_posts(body: dict = Body(...)):
    job = jobs.get(str(body.get("job")))
    clip = next((c for c in job["clips"] if c["id"] == body.get("clip")), None) if job else None
    if not clip or clip.get("status") != "done":
        raise HTTPException(404, "That clip is not ready")
    ids = [str(a) for a in body.get("accounts", [])][:20]
    if body.get("all"):             # one press: every connected account
        ids = [a["id"] for a in accounts.list_accounts()][:20]
        if not ids:
            raise HTTPException(400, "Connect YouTube, TikTok or Instagram first.")
    made = posting.enqueue(job, clip, ids, now=bool(body.get("now")))
    if not made:
        raise HTTPException(400, "Nothing was queued. Pick at least one account this clip has not already gone to.")
    return {"queued": len(made)}


@app.post("/api/posts/{pid}/{action}")
def post_action(pid: str, action: str):
    try:
        {"cancel": posting.cancel, "retry": posting.retry}[action](pid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ================================================================ Chrome Extension Bridge
@app.get("/api/ext/ping")
def ext_ping():
    ctx.uid()
    b = ext_bridge.brief()
    return {
        "ok": True,
        "app": "Upfern",
        "version": _version(),
        "connected": b["connected"],
        "last_seen": b["last_seen"],
        "ext_version": b["version"],
        "export_dir": b["export_dir"],
        "ext_folder": b["ext_dir"],
        "pending": b["pending"],
        "platforms": b["platforms"],
        "by_platform": b["by_platform"],
        "default_platforms": b["default_platforms"],
    }


@app.post("/api/ext/heartbeat")
def ext_heartbeat(body: dict = Body(default={})):
    ctx.uid()
    b = ext_bridge.record_heartbeat(
        version=str(body.get("version") or ""),
        ext_id=str(body.get("ext_id") or ""),
    )
    return {"ok": True, "app": "Upfern", **b}


@app.get("/api/ext/settings")
def ext_settings_get():
    ctx.uid()
    return {
        **ext_bridge.settings(),
        "ext_folder": str(ext_bridge.extension_dir()),
        "connected": ext_bridge.is_connected(),
    }


@app.put("/api/ext/settings")
def ext_settings_put(body: dict = Body(...)):
    ctx.uid()
    try:
        saved = ext_bridge.save_settings(body)
        return {
            **saved,
            "ext_folder": str(ext_bridge.extension_dir()),
            "connected": ext_bridge.is_connected(),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ext/open-folder")
def ext_open_folder(body: dict = Body(default={})):
    ctx.uid()
    kind = str(body.get("kind") or "export").strip().lower()
    if kind not in ("export", "extension"):
        raise HTTPException(400, "Unknown folder kind.")
    try:
        path = ext_bridge.open_in_explorer(kind)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "path": path}


def _ext_queue_single(body: dict, platforms=None):
    jid = str(body.get("job") or "")
    cid = str(body.get("clip") or "")
    job = _job_or_404(jid)
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise HTTPException(404, "No such clip")
    creator = creators.get(job.get("creator")) or creators.DEFAULTS
    try:
        return ext_bridge.queue_clip(
            job,
            clip,
            creator,
            overrides=body.get("overrides"),
            open_browser=bool(body.get("open_browser", True)),
            platforms=platforms,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


def _ext_queue_batch(body: dict, platforms=None):
    raw = body.get("clips") if isinstance(body.get("clips"), list) else []
    pairs = _pairs([p.split(".", 1) for p in raw if isinstance(p, str) and "." in p][:150])
    entries = []
    for jid, cid in pairs:
        job = jobs.get(jid)
        clip = next((c for c in (job or {}).get("clips", []) if c["id"] == cid and c.get("status") == "done"), None)
        if not job or not clip:
            continue
        creator = creators.get(job.get("creator")) or creators.DEFAULTS
        entries.append((job, clip, creator))
    if not entries:
        raise HTTPException(400, "None of the selected clips are ready to export and post.")
    try:
        return ext_bridge.queue_batch(
            entries,
            overrides=body.get("overrides"),
            open_browser=bool(body.get("open_browser", True)),
            platforms=platforms,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ext/post")
def ext_social_post(body: dict = Body(...)):
    return _ext_queue_single(body, platforms=body.get("platforms"))


@app.post("/api/ext/post-batch")
def ext_social_post_batch(body: dict = Body(...)):
    return _ext_queue_batch(body, platforms=body.get("platforms"))


@app.post("/api/ext/youtube/post")
def ext_youtube_post(body: dict = Body(...)):
    return _ext_queue_single(body, platforms=body.get("platforms") or ["youtube"])


@app.post("/api/ext/youtube/post-batch")
def ext_youtube_post_batch(body: dict = Body(...)):
    return _ext_queue_batch(body, platforms=body.get("platforms") or ["youtube"])


@app.get("/api/ext/queue")
def ext_queue_list(platform: str = ""):
    ctx.uid()
    b = ext_bridge.brief()
    return {
        "items": ext_bridge.list_queue(platform=platform or None),
        "connected": b["connected"],
        "by_platform": b["by_platform"],
    }


@app.get("/api/ext/queue/next")
def ext_queue_next(peek: int = 0, platform: str = ""):
    ctx.uid()
    item = ext_bridge.next_pending(platform=platform or None, claim=not bool(peek))
    return {"item": item}


@app.get("/api/ext/queue/{qid}/video")
def ext_queue_video(qid: str):
    ctx.uid()
    try:
        path = ext_bridge.video_path(qid)
    except (KeyError, FileNotFoundError) as e:
        raise HTTPException(404, str(e))
    return FileResponse(path, media_type="video/mp4", filename=path.name, headers={"Cache-Control": "private, no-store"})


@app.post("/api/ext/queue/{qid}/status")
def ext_queue_status(qid: str, body: dict = Body(...)):
    ctx.uid()
    try:
        item = ext_bridge.update_status(qid, body)
        return {"ok": True, "item": item}
    except KeyError:
        raise HTTPException(404, "Queue item not found.")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ext/queue/{qid}/retry")
def ext_queue_retry(qid: str):
    ctx.uid()
    try:
        item = ext_bridge.retry_item(qid)
        return {"ok": True, "item": item}
    except KeyError:
        raise HTTPException(404, "Queue item not found.")


@app.post("/api/ext/queue/{qid}/cancel")
def ext_queue_cancel(qid: str):
    ctx.uid()
    try:
        item = ext_bridge.cancel_item(qid)
        return {"ok": True, "item": item}
    except KeyError:
        raise HTTPException(404, "Queue item not found.")


@app.delete("/api/ext/queue/{qid}")
def ext_queue_delete(qid: str):
    ctx.uid()
    if not ext_bridge.delete_item(qid):
        raise HTTPException(404, "Queue item not found.")
    return {"ok": True}


def _env_read() -> dict[str, str]:
    f = config.ROOT / ".env"
    out = {}
    if f.is_file():
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _env_write(updates: dict[str, str]):
    f = config.ROOT / ".env"
    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines() if f.is_file() else []
    kept, seen = [], set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                val = updates[k]
                if val:
                    kept.append(f"{k}={val}")
                seen.add(k)
                continue
        kept.append(line)
    for k, val in updates.items():
        if k not in seen and val:
            kept.append(f"{k}={val}")
    f.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


@app.get("/api/supabase/status")
def supabase_status():
    ctx.uid()
    env_map = _env_read()
    cur_db = env_map.get("DATABASE_URL") or config.DATABASE_URL or ""
    is_pg = cur_db.startswith(("postgres://", "postgresql"))
    host = ""
    if is_pg:
        try:
            host = urlparse(cur_db.replace("postgresql+psycopg://", "postgresql://", 1)).hostname or ""
        except Exception:
            host = ""
    supa_url = env_map.get("UPFERN_SUPABASE_URL") or config.env("UPFERN_SUPABASE_URL") or ""
    return {
        "mode": "supabase" if is_pg else "sqlite",
        "connected": is_pg,
        "db_host": host,
        "supabase_url": supa_url,
        "has_anon_key": bool(env_map.get("UPFERN_SUPABASE_ANON_KEY") or config.env("UPFERN_SUPABASE_ANON_KEY")),
        "active_mode": "supabase" if config.DATABASE_URL.startswith("postgresql") else "sqlite",
        "restart_required": bool(is_pg) != config.DATABASE_URL.startswith("postgresql"),
    }


@app.put("/api/supabase/config")
def supabase_config_save(body: dict = Body(...)):
    ctx.uid()
    updates = {}
    if "database_url" in body:
        raw_db = str(body.get("database_url") or "").strip()
        if not raw_db:
            updates["DATABASE_URL"] = ""
        else:
            if raw_db.startswith("postgres://"):
                raw_db = "postgresql+psycopg://" + raw_db[len("postgres://"):]
            elif raw_db.startswith("postgresql://"):
                raw_db = "postgresql+psycopg://" + raw_db[len("postgresql://"):]
            if not raw_db.startswith("postgresql+psycopg://"):
                raise HTTPException(400, "Connection string must start with postgresql:// or postgres:// (copy from Supabase -> Project Settings -> Database).")
            # Quick test connection when requested (or always on save) so typo errors surface immediately
            if body.get("test", True):
                try:
                    import sqlalchemy as sa
                    eng = sa.create_engine(raw_db, connect_args={"connect_timeout": 6})
                    with eng.connect() as c:
                        c.execute(sa.text("SELECT 1"))
                    eng.dispose()
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(400, f"Could not connect to Supabase Postgres: {safe.friendly(e, 180)}")
            updates["DATABASE_URL"] = raw_db
    if "supabase_url" in body:
        su = str(body.get("supabase_url") or "").strip().rstrip("/")
        if su and not re.match(r"^https://[\w\-]+\.supabase\.(co|in)$", su, re.I):
            raise HTTPException(400, "Supabase Project URL must look like https://xxxx.supabase.co")
        updates["UPFERN_SUPABASE_URL"] = su
    if "supabase_anon_key" in body:
        updates["UPFERN_SUPABASE_ANON_KEY"] = re.sub(r"[\r\n\s]", "", str(body.get("supabase_anon_key") or ""))[:400]
    _env_write(updates)
    return {"ok": True, **supabase_status(), "message": "Saved to app/.env. Restart Upfern to apply changes."}


@app.post("/api/alerts/test")
def test_alert():
    if not alerts.configured():
        raise HTTPException(400, "Save a Discord webhook or Telegram details first.")
    auth_problem = None
    try:
        auth.throttle("alert:" + ctx.uid(), 10, 600)
    except auth.AuthError as e:
        auth_problem = str(e)
    if auth_problem:
        raise HTTPException(429, auth_problem)
    problems = alerts.send("Upfern: test message. Alerts are working.")
    if problems:
        raise HTTPException(400, "; ".join(problems))
    return {"ok": True}


# ================================================================ admin (only accounts marked as admin in the database)
# The gate above already answers "not found" to everyone else; each route checks again so neither can be forgotten.
def _uid_ok(uid: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{6,40}", uid):
        raise HTTPException(404)
    return uid


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    _admin(request)
    return admins.overview()


@app.get("/api/admin/users/{uid}")
def admin_user(request: Request, uid: str):
    _admin(request)
    try:
        return admins.user_detail(_uid_ok(uid))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/admin/users/{uid}/disabled")
def admin_disable(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    try:
        return admins.set_disabled(me_, _uid_ok(uid), body.get("disabled"))
    except admins.Refused as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/admin/users/{uid}/delete")
def admin_delete_user(request: Request, uid: str, body: dict = Body(...)):
    me_ = _admin(request)
    target = auth.get_user(_uid_ok(uid))
    if not target:
        raise HTTPException(404, "No such account")
    if target.id == me_.id:
        raise HTTPException(400, "You can't delete your own account from here.")
    if str(body.get("confirm") or "").strip().lower() != target.email.lower():
        raise HTTPException(400, "Type the account's email address exactly to confirm.")
    try:
        account.delete_account(target.id)
    except account.Busy as e:
        raise HTTPException(409, str(e))
    with db.engine.begin() as c:
        admins._log(c, me_.id, "delete", target.id, "account deleted")
    return {"ok": True}


def main():
    print(f"\n  Upfern is running:  {config.PUBLIC_URL}\n")
    if not config.ACCOUNTS and (config.HTTPS or config.HOST not in ("127.0.0.1", "localhost", "::1")):
        print("  Sign-in is off, so anyone who can reach this address can use Upfern and its AI key.\n"
              "  On a server other people can reach, set PEAKCUT_ACCOUNTS=1 in .env.\n", flush=True)
    if config.env("PEAKCUT_AUTO_RELOAD") == "1":
        # restart by itself whenever the server code changes (pages in web/ never need a restart)
        uvicorn.run("studio.server:app", host=config.HOST, port=config.PORT, log_level="warning", backlog=4096, reload=True,
                    reload_dirs=[str(config.ROOT / "studio")], reload_includes=["*.py"],
                    proxy_headers=bool(config.env("PEAKCUT_TRUST_PROXY")), forwarded_allow_ips="*")
        return
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", backlog=4096,
                proxy_headers=bool(config.env("PEAKCUT_TRUST_PROXY")), forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
