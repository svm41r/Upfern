"""YouTube needs a JavaScript runtime (Deno or Node) plus yt-dlp's challenge solver, or it answers
"No video formats found". ClipKar carries the solver itself (studio/_vendor) and uses Deno or Node when the PC has
one. Without either, YouTube links show how to install Deno (one command)."""
import shutil
import sys
from pathlib import Path

HOW = ("YouTube needs a free helper called Deno on this PC. Open PowerShell, run:  winget install DenoLand.Deno  "
       "then close ClipKar, open it again and try again. (Twitch and Kick work without it.)")


def _vendor_on_path():
    v = str(Path(__file__).with_name("_vendor"))
    if v not in sys.path:
        sys.path.insert(0, v)


def runtime():
    """('deno' or 'node', path) for the first runtime found on this PC, or None."""
    import os
    extra = [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "deno.exe",
             Path.home() / ".deno" / "bin" / "deno.exe"]
    for name in ("deno", "node"):
        p = shutil.which(name)
        if p:
            return name, p
    for f in extra:
        if f.is_file():
            return "deno", str(f)
    for f in (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages").glob("DenoLand.Deno*/deno.exe"):
        return "deno", str(f)
    return None


def install():
    """Make every yt-dlp call in ClipKar use the PC's runtime and the carried solver."""
    _vendor_on_path()
    import yt_dlp
    if getattr(yt_dlp.YoutubeDL, "_ck_js", False):
        return
    orig = yt_dlp.YoutubeDL.__init__

    def init(self, params=None, *a, **k):
        params = dict(params or {})
        if "js_runtimes" not in params:
            rt = runtime()
            if rt:
                params["js_runtimes"] = {rt[0]: {"path": rt[1]}}
        return orig(self, params, *a, **k)

    yt_dlp.YoutubeDL.__init__ = init
    yt_dlp.YoutubeDL._ck_js = True
