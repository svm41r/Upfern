"""Upfern as a desktop app: its own window, no browser and no terminal.

    pythonw -m studio.desktop

Starts the server in this process if it is not already running, waits for it, and shows it in a window."""
import ctypes
import json
import os
import subprocess
from urllib.request import urlopen
import sys
import threading
import time

from . import config, ext_bridge

URL = f"http://127.0.0.1:{config.PORT}"
ICON = config.ROOT / "web" / "brand" / "upfern.ico"


def _up() -> bool:
    try:
        with urlopen(URL + "/healthz", timeout=3) as response:
            return json.loads(response.read(2048)).get("app") == "upfern"
    except (OSError, ValueError, AttributeError):
        return False


def _server():
    import uvicorn
    from .server import app
    return uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.PORT,
                                         log_level="warning", timeout_graceful_shutdown=5))


def _exit_if_workers_are_stuck():
    # A media worker can block Python's exit while it waits for an external encoder.
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    os._exit(0)


def _icon(window):
    """Windows only: the window and taskbar show Upfern's icon instead of Python's."""
    try:
        from System import Action                # pythonnet, which pywebview already needs on Windows
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(ICON)))))
    except Exception:  # noqa: BLE001 - only the icon is lost
        pass


LOGIN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true"
             "&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26next%3D%252F")
SIGNED_IN = ("LOGIN_INFO", "SAPISID", "__Secure-3PAPISID")


def _netscape(cookies) -> str:
    """Cookies from the window, written the way yt-dlp reads them (the Netscape cookies.txt layout)."""
    from email.utils import parsedate_to_datetime
    lines = ["# Netscape HTTP Cookie File", "# Saved by Upfern from its YouTube sign-in window. Do not share this file.", ""]
    for jar in cookies:
        for name, m in jar.items():
            dom = m["domain"] or ".youtube.com"
            try:
                exp = int(parsedate_to_datetime(m["expires"]).timestamp()) if m["expires"] else 0
            except (TypeError, ValueError):
                exp = 0
            lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE", m["path"] or "/",
                                     "TRUE" if m["secure"] else "FALSE", str(max(0, exp)), name, m.value]))
    return "\n".join(lines) + "\n"


class Api:
    """What the Upfern page can ask the desktop window to do (window.pywebview.api.*)."""

    def __init__(self):
        self.login = None

    def quit(self):
        import webview
        for window in reversed(list(webview.windows)):
            window.destroy()

    def pick_export_folder(self):
        """Show a native folder picker for saving exported YouTube clips."""
        import webview
        if not webview.windows:
            return {"ok": False, "cancelled": True}
        cur = ext_bridge.settings().get("export_dir") or ""
        res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=cur)
        if not res:
            return {"ok": False, "cancelled": True}
        picked = res[0] if isinstance(res, (list, tuple)) else str(res)
        saved = ext_bridge.save_settings({"export_dir": picked})
        return {"ok": True, "export_dir": saved["export_dir"]}

    def youtube_login(self):
        """Open a YouTube sign-in window. When the person is signed in, the login is saved and the window closes."""
        import webview
        if self.login:
            return {"ok": True, "open": True}
        win = webview.create_window("Connect YouTube to Upfern", LOGIN_URL, width=520, height=760, min_size=(420, 600))
        self.login = win

        def watch():
            saved = False
            try:
                while win in webview.windows:
                    time.sleep(2)
                    url = win.get_current_url() or ""
                    if "youtube.com" not in url:
                        continue
                    jars = win.get_cookies() or []
                    names = {n for j in jars for n in j.keys()}
                    if any(n in names for n in SIGNED_IN):
                        f = config.DATA_DIR / "youtube_cookies.txt"
                        f.write_text(_netscape(jars), encoding="utf-8")
                        saved = True
                        time.sleep(1.5)
                        win.destroy()
                        break
            except Exception:  # noqa: BLE001 - the window was closed while being looked at
                pass
            finally:
                self.login = None
                try:
                    import json
                    for w in webview.windows:
                        w.evaluate_js(f"window.dispatchEvent(new CustomEvent('ck-youtube', {{detail: {json.dumps({'saved': saved})}}}))")
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=watch, daemon=True).start()
        return {"ok": True}


def main():
    if sys.platform == "win32":
        # without its own id, Windows groups the window under Python on the taskbar
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Upfern.App")
    if sys.stdout is None:                       # pythonw has nowhere to print
        sys.stdout = sys.stderr = open(os.devnull, "w")
    server = None
    ready = _up()
    if not ready:
        server = _server()
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 45
        while worker.is_alive() and not server.started and time.monotonic() < deadline:
            time.sleep(0.25)
        ready = server.started
    if not ready:
        message = f"Upfern could not start its own server on {URL}. Check whether the port is already in use."
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(0, message, "Upfern", 0x10)
        raise RuntimeError(message)
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    win = webview.create_window("Upfern", URL + "/app", width=1440, height=900, min_size=(1040, 680),
                                background_color="#0B0D10", text_select=True, js_api=Api())
    def close_auxiliary_windows():
        for window in list(webview.windows):
            if window is not win:
                window.destroy()

    win.events.closed += close_auxiliary_windows
    try:
        webview.start(_icon, win, private_mode=False, storage_path=str(config.DATA_DIR / "window"))
    finally:
        if server is not None:
            server.should_exit = True
            deadline = threading.Timer(12, _exit_if_workers_are_stuck)
            deadline.daemon = True
            deadline.start()
            worker.join(timeout=8)


if __name__ == "__main__":
    main()
