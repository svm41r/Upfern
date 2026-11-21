"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import config
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H
# Other shapes a clip can be made in. The one being made is kept per thread (several clips render at once), and the
# functions below read it with `W, H = _wh()`.
ASPECTS = {"9:16": (W, H), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}
_cv = threading.local()


def _wh():
    return getattr(_cv, "wh", (W, H))


# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
    "yellow": {"label": "Viral Yellow", "font": "Poppins ExtraBold", "size": 128, "base": "00E5FF", "active": "FFFFFF",
               "strong": "00E5FF", "outline": 8, "pop": 112, "upper": True, "per": 3},
    "neon": {"label": "Neon", "font": "Archivo Black", "size": 120, "base": "FFFFFF", "active": "14FF39",
             "strong": "FF4DF0", "outline": 6, "pop": 110, "upper": True, "per": 2},
    # `ol` is the outline colour, `shadow` a drop shadow in pixels, `box` draws each word on a solid badge
    "redline": {"label": "Red Line", "font": "Anton", "size": 150, "base": "FFFFFF", "active": "3F2DFF",
                "strong": "3F2DFF", "outline": 7, "pop": 114, "upper": True, "per": 2, "shadow": 4},
    "fire": {"label": "Fire", "font": "Anton", "size": 146, "base": "FFFFFF", "active": "006AFF",
             "strong": "00D0FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "ice": {"label": "Ice", "font": "Poppins ExtraBold", "size": 124, "base": "FFFFFF", "active": "FFE15C",
            "strong": "FFE15C", "outline": 7, "pop": 110, "upper": True, "per": 3, "ol": "3A1A0A"},
    "pink": {"label": "Pink Pop", "font": "Archivo Black", "size": 118, "base": "FFFFFF", "active": "A64DFF",
             "strong": "A64DFF", "outline": 6, "pop": 112, "upper": True, "per": 3},
    "mint": {"label": "Mint", "font": "Poppins ExtraBold", "size": 104, "base": "FFFFFF", "active": "85FF00",
             "strong": "85FF00", "outline": 5, "pop": 106, "upper": False, "per": 4},
    "boxed": {"label": "Boxed", "font": "Poppins ExtraBold", "size": 92, "base": "FFFFFF", "active": "00E5FF",
              "strong": "00E5FF", "outline": 12, "pop": 104, "upper": True, "per": 3, "ol": "141414", "box": True},
    "soft": {"label": "Soft", "font": "Poppins ExtraBold", "size": 88, "base": "FFFFFF", "active": "FFFFFF",
             "strong": "FFFFFF", "outline": 2, "pop": 103, "upper": False, "per": 5, "shadow": 3},
    "comic": {"label": "Comic Pop", "font": "Bangers Regular", "size": 144, "base": "FFFFFF", "active": "00E5FF",
              "strong": "00E5FF", "outline": 8, "pop": 115, "upper": True, "per": 3},
    "cinematic": {"label": "Cinema", "font": "Bebas Neue Regular", "size": 134, "base": "FFFFFF", "active": "245BFF",
                  "strong": "EEE692", "outline": 5, "pop": 106, "upper": True, "per": 4, "shadow": 3},
    "marker": {"label": "Marker", "font": "Permanent Marker Regular", "size": 112, "base": "FFFFFF", "active": "245BFF",
               "strong": "14FF39", "outline": 6, "pop": 110, "upper": False, "per": 3},
    "editorial": {"label": "Editorial", "font": "Instrument Serif Regular", "size": 118, "base": "FFFFFF", "active": "EEE692",
                  "strong": "245BFF", "outline": 3, "pop": 105, "upper": False, "per": 4, "shadow": 3},
    "subtitle": {"label": "Subtitles", "font": "Poppins ExtraBold", "size": 74, "base": "FFFFFF", "active": "FFFFFF",
                 "strong": "FFFFFF", "outline": 4, "pop": 100, "upper": False, "per": 6, "shadow": 3},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 960}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
TEXT_KEYS = ("cap_color", "cap_hi", "cap_ol", "cap_stroke", "cap_shadow", "cap_box", "hook_ink", "hook_boxc", "hook_nobox")
CAP_MAX_W = 960   # captions never run wider than this, whatever is said


_fonts = {"at": 0.0, "list": {}}


def _full_name(path: Path) -> str:
    """The font's own full name ("Montserrat Black Italic"): the name the caption renderer finds it by."""
    import struct
    d = path.read_bytes()
    n = struct.unpack(">H", d[4:6])[0]
    tab = {d[12 + 16 * i: 16 + 16 * i]: struct.unpack(">II", d[20 + 16 * i: 28 + 16 * i]) for i in range(n)}
    off = tab[b"name"][0]
    count, strings = struct.unpack(">HH", d[off + 2: off + 6])
    best = {}
    for i in range(count):
        plat, enc, lang, nid, ln, so = struct.unpack(">6H", d[off + 6 + 12 * i: off + 18 + 12 * i])
        if nid in (1, 2, 4) and (plat == 3 or (plat == 1 and nid not in best)):
            raw = d[off + strings + so: off + strings + so + ln]
            txt = raw.decode("utf-16-be" if plat == 3 else "latin-1", "ignore").strip()
            if txt and (plat != 3 or lang in (0x409, 0x809) or nid not in best):
                best[nid] = txt
    fam, sub = best.get(1, path.stem), best.get(2, "Regular")
    return best.get(4) or (fam if sub.lower() == "regular" else f"{fam} {sub}")


def fonts() -> dict:
    """Every font captions can use: {key: {"label", "name", "path"}}. The key is the file's name without its ending.
    The bundled ones come first; anything dropped into the data folder's fonts folder is added."""
    import time
    if time.time() - _fonts["at"] < 15 and _fonts["list"]:
        return _fonts["list"]
    out = {}
    for folder in (Path(__file__).resolve().parent / "fonts", config.FONTS_DIR):
        for f in sorted(folder.glob("*"), key=lambda p: p.name.lower()):
            if f.suffix.lower() not in (".ttf", ".otf") or f.stem in out:
                continue
            try:
                name = _full_name(f)
            except Exception:  # noqa: BLE001 - not a font this can read
                continue
            out[f.stem] = {"label": name, "name": name, "path": str(f)}
    _fonts.update(at=time.time(), list=out)
    return out


def _font_path(name: str) -> Path:
    """A font by its style name ("Anton"), its key or its full name."""
    if name in FONT_FILES:
        return Path(__file__).resolve().parent / "fonts" / FONT_FILES[name]
    all_ = fonts()
    hit = all_.get(name) or next((v for v in all_.values() if v["name"] == name), None)
    return Path(hit["path"]) if hit else Path(__file__).resolve().parent / "fonts" / FONT_FILES["Poppins ExtraBold"]


def _picked(attr: str, default: str) -> str:
    """The font the clip asks for (as the name the renderer knows it by), or the style's own."""
    hit = fonts().get(getattr(_cv, attr, "") or "")
    return hit["name"] if hit else default


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = _font_path(name)
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    src = Path(__file__).resolve().parent / "fonts"
    for f in src.glob("*.ttf"):
        if not (config.FONTS_DIR / f.name).exists():
            shutil.copyfile(f, config.FONTS_DIR / f.name)


LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "split": "Gameplay underneath", "cam": "Facecam on top (gameplay fills the frame)",
           "facegame": "Streamer + game (wide gameplay with a soft background)"}
GAMEPLAY_DIR = config.DATA_DIR / "gameplay"      # any .mp4 dropped in here can run under a clip


def gameplay_files() -> list:
    return sorted(f.name for f in GAMEPLAY_DIR.glob("*.mp4")) if GAMEPLAY_DIR.is_dir() else []


def _gameplay(clip: dict, dur: float):
    """(file, where to start in it) for the gameplay under this clip, or None when there is none to use."""
    names = gameplay_files()
    if not names:
        return None
    seed = sum(ord(c) for c in str(clip.get("id", "")) + str(int(clip.get("start", 0))))
    name = clip.get("gameplay") if clip.get("gameplay") in names else names[seed % len(names)]
    return GAMEPLAY_DIR / name, float(seed % max(1, int(100 - min(dur, 99))))


def _retime_keys(keys: list, keep) -> list:
    """Face positions on the clock of a tightened clip: the cut-out stretches are gone, the rest moves up."""
    if not keep:
        return keys
    out, at = [], 0.0
    for a, b in keep:
        out += [[round(at + t - a, 2), x] for t, x in keys if a <= t <= b]
        at += b - a
    return out or keys[:1]


def _follow(keys: list, wf: float) -> str:
    """An ffmpeg expression for where the crop sits (0 to 1) over time, gliding between the face positions."""
    if wf >= 1 or not keys:
        return "0.5"
    pts = [(t, min(1.0, max(0.0, (x - wf / 2) / (1 - wf)))) for t, x in keys]
    if len(pts) == 1:
        return f"{pts[0][1]:.4f}"
    parts = [f"lt(t,{pts[0][0]:.2f})*{pts[0][1]:.4f}", f"gte(t,{pts[-1][0]:.2f})*{pts[-1][1]:.4f}"]
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        if t1 - t0 < 0.01:
            continue
        parts.append(f"gte(t,{t0:.2f})*lt(t,{t1:.2f})*({a:.4f}+{(b - a) / (t1 - t0):.5f}*(t-{t0:.2f}))")
    return "+".join(parts)


def _retime_cam_keys(keys: list, keep) -> list:
    """Manual facecam keyframes moved onto the clock of a tightened/cut clip."""
    if not keep:
        return keys
    out, at = [], 0.0
    for a, b in keep:
        out += [[round(at + t - a, 3), box] for t, box in keys if a <= t <= b]
        at += b - a
    return out or keys[:1]


def _interp_1d(pts: list) -> str:
    """Piecewise-linear ffmpeg expression in `t` across [(t0, v0), (t1, v1), ...]."""
    dedup = []
    for t, v in sorted(pts, key=lambda x: x[0]):
        if dedup and abs(t - dedup[-1][0]) < 0.01:
            dedup[-1] = (t, v)
        else:
            dedup.append((t, v))
    if not dedup:
        return "0"
    if len(dedup) == 1:
        return f"{dedup[0][1]:.2f}"
    parts = [f"lt(t,{dedup[0][0]:.3f})*{dedup[0][1]:.2f}", f"gte(t,{dedup[-1][0]:.3f})*{dedup[-1][1]:.2f}"]
    for (t0, a), (t1, b) in zip(dedup, dedup[1:]):
        if t1 - t0 < 0.01:
            continue
        slope = (b - a) / (t1 - t0)
        parts.append(f"gte(t,{t0:.3f})*lt(t,{t1:.3f})*({a:.2f}+({slope:.4f})*(t-{t0:.3f}))")
    return "+".join(parts)


def _cam_chain(v_in: str, cam: tuple, cam_keys: list, src_w: int, src_h: int, W: int, fg_h: int, dur: float) -> str:
    """Crop the manual facecam box across time: static, smooth panned crop, or segment switch if box size changes."""
    if cam_keys and len(cam_keys) >= 2 and src_w > 0 and src_h > 0:
        ks = sorted(cam_keys, key=lambda x: x[0])
        ws = [b[2] for _, b in ks]
        hs = [b[3] for _, b in ks]
        if max(ws) / max(16, min(ws)) <= 1.15 and max(hs) / max(16, min(hs)) <= 1.15:
            bw = max(16, min(src_w // 2 * 2, sorted(ws)[len(ws) // 2] // 2 * 2))
            bh = max(16, min(src_h // 2 * 2, sorted(hs)[len(hs) // 2] // 2 * 2))
            max_x, max_y = max(0, src_w - bw), max(0, src_h - bh)
            pts_x = [(t, min(max(0.0, px + pw / 2 - bw / 2), float(max_x))) for t, (px, py, pw, ph) in ks]
            pts_y = [(t, min(max(0.0, py + ph / 2 - bh / 2), float(max_y))) for t, (px, py, pw, ph) in ks]
            x_expr = f"clip({_interp_1d(pts_x)},0,{max_x})"
            y_expr = f"clip({_interp_1d(pts_y)},0,{max_y})"
            return (f"{v_in}crop={bw}:{bh}:'{x_expr}':'{y_expr}',"
                    f"scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h},setsar=1[cam];")
        # Box dimensions changed significantly between manual keyframes: split and composite per-segment crops
        segs = ks[:16]
        k_n = len(segs)
        splits = "".join(f"[ck{i}]" for i in range(k_n))
        chain = f"{v_in}split={k_n}{splits};"
        for i, (_, box) in enumerate(segs):
            px, py, pw, ph = box
            out_lbl = "[cam]" if k_n == 1 else ("[cam0]" if i == 0 else f"[cks{i}]")
            chain += (f"[ck{i}]crop={pw}:{ph}:{px}:{py},"
                      f"scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h},setsar=1{out_lbl};")
        cur_cam = "[cam0]"
        for i in range(1, k_n):
            ta = max(0.0, float(segs[i][0]))
            tb = max(ta + 0.05, float(segs[i + 1][0]) if i + 1 < k_n else dur + 2.0)
            nxt = "[cam]" if i == k_n - 1 else f"[cam{i}]"
            chain += f"{cur_cam}[cks{i}]overlay=0:0:enable='between(t,{ta:.3f},{tb:.3f})'{nxt};"
            cur_cam = nxt
        return chain
    return (f"{v_in}crop={cam[2]}:{cam[3]}:{cam[0]}:{cam[1]},"
            f"scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h},setsar=1[cam];")



def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


_CAP_EMOJI = re.compile(
    r"([#*0-9]\ufe0f?\u20e3|[\U0001f1e6-\U0001f1ff]{2}|"
    r"[\U0001f000-\U0001faff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff]\ufe0f?"
    r"[\U0001f3fb-\U0001f3ff]?(?:\u200d[\U0001f000-\U0001faff\u2600-\u27bf]\ufe0f?[\U0001f3fb-\U0001f3ff]?)*"
    r"[\U000e0020-\U000e007e]*\U000e007f?|[©®™]\ufe0f)"
)


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (w.get("gs") or cur[-1].get("br") or (not w.get("join") and (
                    len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"))):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def cam_px(cam, src_w, src_h):
    """The facecam box in whole, even pixels of the picture, or None when it is missing or too small to use."""
    try:
        x, y, w, h = (min(1.0, max(0.0, float(v))) for v in cam)
    except (TypeError, ValueError):
        return None
    w, h = min(w, 1 - x), min(h, 1 - y)
    if w < 0.04 or h < 0.04:
        return None
    return int(x * src_w / 2) * 2, int(y * src_h / 2) * 2, max(16, int(w * src_w / 2) * 2), max(16, int(h * src_h / 2) * 2)


MASK_SHAPES = ("rect", "round", "circle")


def mask_px(mask, src_w, src_h):
    """A cut-out in whole pixels: what to cut from the picture, how big it is on the clip, and where it goes."""
    if not isinstance(mask, dict):
        return None
    cut = cam_px(mask.get("src"), src_w, src_h)
    if not cut:
        return None
    cw, ch = _wh()
    try:
        w = min(1.0, max(0.08, float(mask.get("w", 0.5))))
        cx, cy = float(mask.get("x", 0.5)), float(mask.get("y", 0.25))
    except (TypeError, ValueError):
        return None
    mw = int(w * cw / 2) * 2
    mh = int(mw * cut[3] / cut[2] / 2) * 2
    if mh > ch:                                     # taller than the clip: shrink it to fit, same shape
        mw, mh = int(mw * ch / mh / 2) * 2, ch // 2 * 2
    ox = int(min(max(cx * cw - mw / 2, -mw * 0.5), cw - mw * 0.5))
    oy = int(min(max(cy * ch - mh / 2, -mh * 0.5), ch - mh * 0.5))
    return {"cut": cut, "size": (max(16, mw), max(16, mh)), "at": (ox, oy),
            "shape": mask.get("shape") if mask.get("shape") in MASK_SHAPES else "round", "border": bool(mask.get("border"))}


def _minus(keep, cuts):
    """The stretches in keep with the cut parts taken out (all in seconds from the clip's start)."""
    out = []
    for a, b in keep:
        pieces = [(a, b)]
        for ca, cb in cuts:
            nxt = []
            for x, y in pieces:
                if cb <= x or ca >= y:
                    nxt.append((x, y))
                    continue
                if ca > x:
                    nxt.append((x, ca))
                if cb < y:
                    nxt.append((cb, y))
            pieces = nxt
        out += [(round(x, 3), round(y, 3)) for x, y in pieces if y - x >= 0.05]
    return out


def _clip_cuts(clip):
    """The parts deleted from inside the clip, in seconds from its start."""
    start, end = clip["start"], clip["end"]
    out = []
    for c in clip.get("cuts") or []:
        try:
            a, b = max(start, float(c[0])), min(end, float(c[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if b - a >= 0.05:
            out.append((a - start, b - start))
    return out


def _out_t(rel, keep):
    """A time in seconds from the clip's start, as a time in the finished clip (auto edit cuts bits out)."""
    if not keep:
        return max(0.0, rel)
    return sum(max(0.0, min(b, rel) - a) for a, b in keep)


def _mask_parts(clip, src_w, src_h, keep):
    """Every cut-out with when it shows in the finished clip: [(from, to, mask_px), ...]."""
    start, end = clip["start"], clip["end"]
    full = _out_t(end - start, keep)
    parts = clip.get("masks")
    if isinstance(parts, list) and parts:
        out = []
        for p in parts:
            m = mask_px(p.get("m"), src_w, src_h)
            a, b = max(start, float(p["a"])), min(end, float(p["b"]))
            if m and b - a > 0.04:
                out.append((round(_out_t(a - start, keep), 3), round(_out_t(b - start, keep), 3), m))
        return out
    m = mask_px(clip.get("mask"), src_w, src_h)
    return [(0.0, full + 1, m)] if m else []


def _mask_pngs(work: Path, m: dict, n: int = 0):
    """The cut-out's shape as a picture (white = keep), and its border as a second one."""
    from PIL import Image, ImageDraw
    w, h = m["size"]
    box, r = [0, 0, w - 1, h - 1], int(min(w, h) * 0.12)
    for name, mode, fill, outline in ((f"mk{n}.png", "L", 255, None), (f"rg{n}.png", "RGBA", None, (255, 255, 255, 255))):
        im = Image.new(mode, (w, h), 0)
        d = ImageDraw.Draw(im)
        kw = {"fill": fill} if outline is None else {"outline": outline, "width": max(4, int(min(w, h) * 0.014))}
        if m["shape"] == "circle":
            d.ellipse(box, **kw)
        elif m["shape"] == "round":
            d.rounded_rectangle(box, radius=r, **kw)
        else:
            d.rectangle(box, **kw)
        im.save(work / name)


def auto_cam(src, start, end, src_w, src_h, top_frac=0.42):
    """The "Streamer + game" template: a box around the streamer's face, shaped like the top panel. None if no face."""
    from . import faces
    try:
        f = faces.find(src, start, end, samples=24) or faces.find(src, max(0.0, start - 20), end + 20, samples=24)
    except Exception:  # noqa: BLE001
        f = None
    if not f:
        return None
    W, H = _wh()
    shape = W / (H * top_frac)                                  # width / height of the top panel
    bw = min(1.0, max(f["w"] * 3.0, 0.18))
    bh = min(1.0, bw * src_w / src_h / shape)
    if bh >= 1.0:
        bh, bw = 1.0, min(1.0, shape * src_h / src_w)
    x = min(max(0.0, f["x"] + f["w"] / 2 - bw / 2), 1 - bw)
    y = min(max(0.0, f["y"] + f["h"] * 0.55 - bh * 0.5), 1 - bh)
    return [round(x, 4), round(y, 4), round(bw, 4), round(bh, 4)]


def geometry(src_w, src_h, layout, cam=None, band_y=0.5, cam_h=None, seam=False, band_s=1.0):
    """Where the video sits in the frame (1080x1920 unless another shape was asked for), and where hook and captions go."""
    W, H = _wh()
    k = H / 1920
    if (src_h >= src_w or src_h * W >= src_w * H) and layout not in ("crop", "split", "cam", "facegame"):
        layout = "crop"  # the video is at least as tall as the frame: nothing to gain from a blurred fill
    if layout in ("cam", "facegame") and cam:
        if cam_h:                    # set by dragging the line between facecam and game in the editor
            th = int(H * min(0.65, max(0.2, float(cam_h))) / 2) * 2
        else:
            th = int(min(H * 0.5, max(H * 0.28, W * cam[3] / cam[2])) / 2) * 2
        if seam:                     # template: headline on the line between face and game, captions in the game
            return layout, th, th, th + int((H - th) * 0.55)
        return layout, th, int(120 * k), th
    if layout == "split":      # the video on top, gameplay underneath, captions on the line between them
        top = H // 2
        return "split", top, int(150 * k), top
    bs = min(1.6, max(0.4, float(band_s or 1.0)))          # the video made smaller or bigger in the editor
    if layout == "blur":
        fg_h = min(H, int(round(W * src_h / src_w * bs / 2)) * 2)
    elif layout == "zoom":
        fg_h = min(H, int(1000 * k * bs / 2) * 2)
    else:
        fg_h = H
    top = int((H - fg_h) * min(1.0, max(0.0, float(band_y if band_y is not None else 0.5))) / 2) * 2   # dragged up or down
    if layout == "crop" or fg_h >= H:
        return "crop", H, int(300 * k), int(1380 * k)
    return layout, fg_h, max(int(230 * k), top - 190), min(int(1640 * k), top + fg_h + 180)


def _bgr(colour) -> str:
    """'#RRGGBB' as the renderer writes colours (BBGGRR), or '' when it is not a colour."""
    c = str(colour or "").lstrip("#")
    return (c[4:6] + c[2:4] + c[0:2]).upper() if re.fullmatch(r"[0-9a-fA-F]{6}", c) else ""


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    W, H = _wh()
    f, size = _Font.get(_picked("hook_font", HOOK["font"])), int(HOOK["size"] * getattr(_cv, "hook_scale", 1.0))
    caps = getattr(_cv, "caps", "")
    hook = hook.upper() if caps == "upper" else hook.lower() if caps == "lower" else hook
    import re as _re
    emo_re = "[\U0001F000-\U0001FAFF\u2600-\u27BF]\uFE0F?(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27BF]\uFE0F?)*"
    emos = _re.findall(emo_re, hook)[:3]          # drawn as colour pictures after the text (the font can't draw them)
    hook = _re.sub(emo_re, "", hook).replace("\uFE0F", "").strip()
    _cv.hook_emojis = []
    words = hook.split()
    if not words:
        return []
    if len(words) > 6:                 # a headline: smaller letters, in up to 4 lines, as wide as the frame allows
        size = int(size * 0.74)

    def wrap(n):                       # n lines with the widest one as narrow as possible (greedy on a target width)
        total = f.width(" ".join(words), size)
        target, out, cur = total / n, [], []
        for w in words:
            if cur and f.width(" ".join(cur + [w]), size) > target * 1.08 and len(out) < n - 1:
                out.append(" ".join(cur))
                cur = []
            cur.append(w)
        out.append(" ".join(cur))
        return out
    lines = [" ".join(words)]
    for n in range(2, 5):
        if max(f.width(l, size) for l in lines) <= HOOK["max_w"] or len(words) < n:
            break
        lines = wrap(n)
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    epx = int(f.em(size) * 1.05)                    # emoji size: a bit taller than a capital letter
    eblock = (epx * len(emos) + int(size * 0.25)) if emos else 0
    widest = max(widest, f.width(lines[-1], size) + eblock)
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    hx = min(max(getattr(_cv, "hook_x", None) or W / 2, bw / 2 + 8), W - bw / 2 - 8)     # dragged there, kept inside the frame
    x0, y0, r = hx - bw / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    look = getattr(_cv, "text", None) or {}
    nobox = True if look.get("hook_nobox") is None else bool(look.get("hook_nobox"))     # default: no box, white text with a black outline
    ink = _bgr(look.get("hook_ink")) or ("FFFFFF" if nobox else HOOK["ink"])
    plain = f"\\c&H{ink}&" + ("\\bord7\\3c&H000000&\\shad2" if nobox else "")       # without its box the hook gets an outline, so it still reads
    ev = [] if nobox else [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{_bgr(look.get('hook_boxc')) or HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        lx = hx - (eblock / 2 if (emos and i == len(lines) - 1) else 0)      # the last line moves left to fit the emoji
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({lx:.0f},{cy:.0f})\\fad(160,0)\\fs{size}{plain}}}{line}")
        if emos and i == len(lines) - 1:
            ex = lx + f.width(line, size) / 2 + size * 0.25 + epx / 2
            ey = y - line_h * (len(lines) - 1) / 2 + i * line_h
            _cv.hook_emojis = [(e, epx, (ex + k * epx, ey)) for k, e in enumerate(emos)]
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, hook_span=None) -> str:
    W, H = _wh()
    st = STYLES.get(style_name, STYLES["bounce"])
    cs = getattr(_cv, "cap_scale", 1.0)
    caps = getattr(_cv, "caps", "")
    st = dict(st, font=_picked("cap_font", st["font"]), upper=st["upper"] if not caps else caps == "upper", lower=caps == "lower")
    look = getattr(_cv, "text", None) or {}      # what was changed by hand in the editor; anything left out stays as the style has it
    for mine, theirs in (("cap_color", "base"), ("cap_hi", "active"), ("cap_ol", "ol")):
        if _bgr(look.get(mine)):
            st[theirs] = _bgr(look[mine])
    if _bgr(look.get("cap_color")):
        st["strong"] = st["base"]
    if look.get("cap_stroke") is not None:
        st["outline"] = max(0, min(20, int(look["cap_stroke"])))
    if look.get("cap_shadow") is not None:
        st["shadow"] = max(0, min(16, int(look["cap_shadow"])))
    if look.get("cap_box") is not None:
        st["box"] = bool(look["cap_box"])
        if st["box"] and st["outline"] < 6:
            st["outline"] = 10                    # a box is drawn as a thick border: it needs some thickness to show
    if cs != 1.0:           # the same style, bigger or smaller: the outline grows with the letters
        st = dict(st, size=max(30, int(st["size"] * cs)), outline=max(1, round(st["outline"] * cs)))
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00{st.get('ol', '000000')},&H80000000,0,0,0,0,100,100,0,0,{3 if st.get('box') else 1},{st['outline']},{st.get('shadow', 0)},5,0,0,0,1
Style: Hook,{_picked("hook_font", HOOK["font"])},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    h0, h1 = hook_span if hook_span is not None else (0.0, dur)
    ev = _hook_events(_clean(hook), h1 - h0, hook_y) if h1 > h0 else []
    if h0 or h1 != dur:
        for i, event in enumerate(ev):
            parts = event.split(",", 3)
            ev[i] = ",".join([parts[0], _ts(h0), _ts(h1), parts[3]])
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = [w for w in inside if w["w"]]
    groups = _groups(inside, st["per"])
    _cv.caption_emojis = []
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t.lower() if st.get("lower") else t)
        pieces = [_CAP_EMOJI.split(t) for t in texts]
        def width(parts, size):
            return sum(round(size * .90) if i % 2 else f.width(part, size) if part else 0
                       for i, part in enumerate(parts))
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6 + (26 if st.get("box") else 0)) * size / st["size"]   # a space, plus room for both outlines
        total = sum(width(parts, size) for parts in pieces) + gap * (len(pieces) - 1)
        if total > CAP_MAX_W:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6 + (26 if st.get("box") else 0)) * size / st["size"]   # a space, plus room for both outlines
            total = sum(width(parts, size) for parts in pieces) + gap * (len(pieces) - 1)
        cy = cap_y - f.cap_shift(size)
        x = min(max((getattr(_cv, "cap_x", None) or W / 2) - total / 2, 12), max(12, W - total - 12))
        p = st["pop"]
        for i, (w, parts) in enumerate(zip(g, pieces)):
            wd = width(parts, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            runs = []
            left = x
            for j, part in enumerate(parts):
                if not part:
                    continue
                if j % 2:
                    px = round(size * .90)
                    _cv.caption_emojis.append((part, px, (left + px / 2, cap_y), g_start, g_end))
                else:
                    px = f.width(part, size)
                    runs.append((f"\\an5\\pos({left + px / 2:.0f},{cy:.0f})\\fs{size}", part))
                left += px
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            for pos, text in runs:
                if a - g_start >= 0.02:
                    ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{text}")
                if b - a >= 0.02:
                    ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                              f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{text}")
                if g_end - b >= 0.02:
                    ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                              f"\\t(0,90,\\fscx100\\fscy100)}}{text}")
    return head + "\n".join(ev) + "\n"


def _box(bw, bh, cx, cy=0.5, s=1.0, *, before_crop="") -> str:
    """Fill a bw x bh box from the picture: scaled to cover it (times `s`, the zoom), then cut out at (cx, cy).
    cx is a number from 0 (hard left) to 1 (hard right), or an expression in t when the crop follows a face."""
    sw, sh = int(round(bw * s / 2)) * 2, int(round(bh * s / 2)) * 2
    x = f"(iw-{bw})*({cx})" if isinstance(cx, str) else f"(iw-{bw})*{min(1.0, max(0.0, float(cx))):.4f}"
    return (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"{before_crop}crop={bw}:{bh}:'{x}':(ih-{bh})*{min(1.0, max(0.0, float(cy))):.4f}")


def _filter(layout, fg_h, cx=0.5, cy=0.5, s=1.0, cam=None, band_y=0.5, band_s=1.0, band_x=0.5,
            v_in="[vmain]", cam_keys=None, src_w=0, src_h=0, dur=0.0):
    W, H = _wh()
    if layout == "facegame" and cam:
        bottom = H - fg_h
        fit = min(W / src_w, bottom / src_h) * s
        fw, fh = max(2, round(src_w * fit / 2) * 2), max(2, round(src_h * fit / 2) * 2)
        gx = f"(W-w)*{float(cx):.5f}"
        gy = f"(H-h)*{float(cy):.5f}"
        return (f"{v_in}split=3[a][b][c];"
                + _cam_chain("[a]", cam, cam_keys, src_w, src_h, W, fg_h, dur)
                + f"[b]{_box(W,bottom,0.5)},gblur=sigma=20,eq=brightness=-0.20:saturation=0.72,setsar=1[back];"
                + f"[c]scale={fw}:{fh},setsar=1[wide];[back][wide]overlay=x={gx}:y={gy}:shortest=1[game];"
                + "[cam][game]vstack=shortest=1[v0];")
    if layout == "cam" and cam:
        return (f"{v_in}split=2[a][b];"
                + _cam_chain("[a]", cam, cam_keys, src_w, src_h, W, fg_h, dur)
                + f"[b]{_box(W, H - fg_h, cx, cy, s)},setsar=1[game];[cam][game]vstack[v0];")
    if layout == "crop":
        return f"{v_in}{_box(W, H, cx, cy, s)},setsar=1[v0];"
    if layout == "split":
        # A 4:5 frame has two 675px halves, so subsample chroma after stacking.
        full_chroma = "format=yuv444p," if fg_h % 2 else ""
        output_format = ",format=yuv420p" if full_chroma else ""
        return (f"{v_in}{_box(W, fg_h, cx, cy, s, before_crop=full_chroma)},setsar=1[top];"
                f"[1:v]fps={config.OUT_FPS},scale={W}:{H - fg_h}:force_original_aspect_ratio=increase,{full_chroma}crop={W}:{H - fg_h},setsar=1[gp];"
                f"[top][gp]vstack=shortest=1{output_format}[v0];")
    bh = int(round(270 * H / W / 2)) * 2
    bs = min(1.6, max(0.4, float(band_s or 1.0)))
    fw = int(W * bs / 2) * 2 if layout == "blur" else int(W * max(1.0, bs) / 2) * 2 if bs > 1 else int(W * bs / 2) * 2
    ox = int(((W - fw) / 2 + (min(1.0, max(0.0, float(band_x if band_x is not None else 0.5))) - 0.5) * W) / 2) * 2
    return (f"{v_in}split=2[a][b];"
            f"[a]scale=270:{bh}:force_original_aspect_ratio=increase,crop=270:{bh},boxblur=6:2,"
            f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
            f"[b]{_box(fw, fg_h, cx, cy, s)}[fg];"
            f"[bg][fg]overlay={ox}:{int((H - fg_h) * min(1.0, max(0.0, float(band_y if band_y is not None else 0.5))) / 2) * 2},setsar=1[v0];")


_qsv_ok = None          # False once the graphics card has failed on a real clip
# Encoders on a graphics card, fastest first. Each is tried once on a blank half-second; the first that works is used.
HW = {"nvenc": ["yuv420p", "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
      "amf": ["nv12", "-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"],
      "qsv": ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]}
_hw_lock = threading.Lock()
_hw = []                # [] = not looked yet, [None] = none works, ["amf"] = use that one


def _hw_encoder():
    if _qsv_ok is False or os.environ.get("PEAKCUT_ENCODER", "").lower() in ("x264", "cpu"):
        return None
    with _hw_lock:
        if not _hw:
            found = None
            for label, enc in HW.items():
                try:
                    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=1080x1920:r=30:d=0.5", "-pix_fmt"]
                                       + enc + ["-f", "null", "-"], capture_output=True, creationflags=NO_WINDOW, timeout=25)
                    if p.returncode == 0:
                        found = label
                        break
                except Exception:  # noqa: BLE001 - no ffmpeg, or it hung: the processor encodes
                    pass
            _hw.append(found)
        return _hw[0]


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _cv.wh = ASPECTS.get(clip.get("aspect"), (W, H))
    cw, ch = _cv.wh
    pos = lambda v: v if isinstance(v, (list, tuple)) and len(v) == 2 else None      # noqa: E731
    _cv.cap_font, _cv.hook_font, _cv.caps = clip.get("font") or "", clip.get("hook_font") or "", clip.get("caps") or ""
    _cv.text = {k: clip.get(k) for k in TEXT_KEYS}
    _cv.cap_x = pos(clip.get("cap_pos")) and float(clip["cap_pos"][0]) * cw
    _cv.hook_x = pos(clip.get("hook_pos")) and float(clip["hook_pos"][0]) * cw
    _cv.cap_scale = min(2.0, max(0.5, float(clip.get("cap_scale") or 1.0)))
    _cv.hook_scale = min(2.0, max(0.5, float(clip.get("hook_scale") or 1.0)))
    try:
        return _render_clip(src, out_dir, name, clip, words, src_w, src_h)
    finally:
        _cv.wh, _cv.cap_scale, _cv.hook_scale = (W, H), 1.0, 1.0
        _cv.cap_font = _cv.hook_font = _cv.caps = ""
        _cv.text = None
        _cv.cap_x = _cv.hook_x = None


def _emoji_font():
    """Apple-style emoji if the person put Apple's emoji font in data/fonts (it can't ship with ClipKar), else Windows'."""
    for name in ("AppleColorEmoji.ttc", "AppleColorEmoji.ttf", "Apple Color Emoji.ttc", "Apple Color Emoji.ttf", "NotoColorEmoji.ttf"):
        f = config.DATA_DIR / "fonts" / name
        if f.is_file():
            return f
    return Path(r"C:\Windows\Fonts\seguiemj.ttf")


EMOJI_FONT = _emoji_font()


def _sfx_parts(clip, keep):
    """Meme sounds on the clip: [(file, when in the finished clip, dB)]. Sounds in a deleted part are dropped."""
    from . import sfx as sfxmod
    start, end = clip["start"], clip["end"]
    master = clip.get("sfx_db")
    master = sfxmod.settings()["db"] if master is None else max(-40, min(6, float(master)))
    cuts = [(a + start, b + start) for a, b in (_clip_cuts(clip) or [])]
    out = []
    for s in (clip.get("sfx") or [])[:12]:
        f = sfxmod.local(str(s.get("file") or ""))
        try:
            t = float(s.get("t"))
        except (TypeError, ValueError):
            continue
        if not f or not start <= t < end - 0.2 or any(a <= t < b for a, b in cuts):
            continue
        db = master + max(-20, min(10, float(s.get("db") or 0)))
        out.append((str(f), round(_out_t(t - start, keep), 3), round(db, 1)))
    return out


def _emoji_parts(clip, keep):
    """Emojis placed by hand: [(emoji, size_px, (x, y) centre, from, to) in the finished clip's time]."""
    start, end = clip["start"], clip["end"]
    cw, ch = _wh()
    out = []
    for t in clip.get("texts") or []:
        if not isinstance(t, dict) or not t.get("emoji") or not str(t.get("t") or "").strip():
            continue
        a, b = max(start, float(t.get("a", start))), min(end, float(t.get("b", end)))
        if b - a < 0.1:
            continue
        px = max(24, min(1600, int(float(t.get("size", 220)) * ch / 1920)))
        out.append((str(t["t"]).strip()[:8], px, (float(t.get("x", 0.5)) * cw, float(t.get("y", 0.4)) * ch),
                    round(_out_t(a - start, keep), 3), round(_out_t(b - start, keep), 3)))
    return out[:16]


APPLE_EMOJI = "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@16.0.0/img/apple/64/{}.png"


def _apple_emoji(emoji: str):
    """The iPhone-style picture of an emoji (from the emoji-data project), fetched once and kept in data/emoji.
    None when offline or when that emoji has no picture there (the Windows one is drawn instead)."""
    import httpx
    codes = [f"{ord(ch):x}" for ch in emoji]
    names = ["-".join(codes), "-".join(c for c in codes if c != "fe0f")]
    d = config.DATA_DIR / "emoji" / "apple"
    d.mkdir(parents=True, exist_ok=True)
    for n in dict.fromkeys(names):
        f = d / f"{n}.png"
        if f.is_file() and f.stat().st_size > 200:
            return f
        if (d / f"{n}.none").is_file():
            continue
        try:
            r = httpx.get(APPLE_EMOJI.format(n), timeout=10)
            if r.status_code == 200 and r.content[1:4] == b"PNG":
                f.write_bytes(r.content)
                return f
            if r.status_code == 404:
                (d / f"{n}.none").write_text("")
        except Exception:  # noqa: BLE001 - offline: the Windows emoji is used
            return None
    return None


def _emoji_png(path: Path, emoji: str, px: int):
    """One emoji as a picture with a see-through background, in colour, cropped to the emoji."""
    from PIL import Image, ImageDraw, ImageFont
    apple = _apple_emoji(emoji) if px <= 160 else None        # the iPhone pictures are 64 px: sharp up to headline size
    if apple:
        im = Image.open(apple).convert("RGBA")
        box = im.getbbox()
        if box:
            im = im.crop(box)
        k = px / max(1, im.height)
        im = im.resize((max(2, int(im.width * k)), max(2, int(im.height * k))), Image.LANCZOS)
        im.save(path)
        return im.size
    size = 160 if "Apple" in EMOJI_FONT.name else 109          # the emoji font's own picture size: drawn there, then scaled
    f = ImageFont.truetype(str(EMOJI_FONT), size) if EMOJI_FONT.is_file() else ImageFont.load_default()
    im = Image.new("RGBA", (size * 3, size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((size // 4, size // 4), emoji, font=f, embedded_color=True)
    box = im.getbbox()
    if box:
        im = im.crop(box)
    k = px / max(1, im.height)
    im = im.resize((max(2, int(im.width * k)), max(2, int(im.height * k))), Image.LANCZOS)
    im.save(path)
    return im.size


def _extra_text(ass: str, texts, start: float, end: float, keep) -> str:
    """Place text over its visible source range, then map it through any cuts."""
    cw, ch = _wh()
    ev = []
    for t in [t for t in (texts or []) if isinstance(t, dict) and not t.get("emoji") and not t.get("sticker")][:8]:
        if not isinstance(t, dict):
            continue
        words = _clean(str(t.get("t") or ""))[:80]
        if not words:
            continue
        try:
            x, y = float(t.get("x", 0.5)) * cw, float(t.get("y", 0.5)) * ch
            size, stroke = max(20, min(400, int(t.get("size", 90)))), max(0, min(20, int(t.get("stroke", 5))))
        except (TypeError, ValueError):
            continue
        font = fonts().get(str(t.get("font") or ""))
        fn = f"\\fn{font['name']}" if font else ""
        a = max(start, float(t.get("a", start)))
        b = min(end, float(t.get("b", end)))
        a, b = _out_t(a - start, keep), _out_t(b - start, keep)
        if b - a < .01:
            continue
        ev.append(f"Dialogue: 6,{_ts(a)},{_ts(b)},Extra,,0,0,0,,{{\\an5\\pos({x:.0f},{y:.0f}){fn}\\fs{size}\\c&H{_bgr(t.get('color')) or 'FFFFFF'}&"
                  f"\\3c&H{_bgr(t.get('ol')) or '000000'}&\\bord{stroke}\\shad{2 if t.get('shadow') else 0}}}{words}")
    if not ev:
        return ass
    style = "Style: Extra,Poppins ExtraBold,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,5,0,5,0,0,0,1"
    return ass.replace("\n\n[Events]", f"\n{style}\n\n[Events]", 1) + "\n".join(ev) + "\n"


def _render_clip(src, out_dir, name, clip, words, src_w, src_h) -> dict:
    start, end = clip["start"], clip["end"]
    want = clip.get("layout", "blur")
    if want == "cam" and clip.get("hook_seam"):
        want = "facegame"             # older editor drafts stored Streamer + Game under the Facecam layout
    cam = cam_px(clip.get("cam_box"), src_w, src_h) if want in ("cam", "facegame") else None
    if want in ("cam", "facegame") and not cam:
        want = "crop"
    gp = _gameplay(clip, end - start) if want == "split" else None
    if (want == "split" and not gp):
        want = "crop"                 # no gameplay on this machine, or no camera box drawn: fill the screen
    cam_height = (clip.get("cam_h") or .30) if want == "facegame" else clip.get("cam_h")
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, want, cam, band_y=clip.get("band_y", 0.5), cam_h=cam_height,
                                           seam=bool(clip.get("hook_seam")), band_s=clip.get("band_s", 1.0))
    _, ch = _wh()
    if isinstance(clip.get("hook_pos"), (list, tuple)) and len(clip["hook_pos"]) == 2:     # placed by hand in the editor
        hook_y = int(float(clip["hook_pos"][1]) * ch)
    if isinstance(clip.get("cap_pos"), (list, tuple)) and len(clip["cap_pos"]) == 2:
        cap_y = int(float(clip["cap_pos"][1]) * ch)
    overlay = clip.get("overlay_mode")
    if overlay in ("captions", "hook"):
        if layout in ("cam", "facegame", "split"):
            hook_y = cap_y = fg_h
            _cv.hook_x = _cv.cap_x = None
        else:
            hook_y = cap_y
            _cv.hook_x = _cv.cap_x
    a_off = max(-30.0, min(30.0, float(clip.get("audio_offset") or 0.0)))
    if a_off != 0.0 and clip.get("audio_sync_caps") is True:      # opt-in only: captions follow the audio layer
        words = [dict(w, s=w["s"] + a_off, e=w["e"] + a_off) for w in words]
    auto_keep = None
    if clip.get("auto_cut_enabled") and not clip.get("mute_audio"):
        protection = clip.get("auto_cut_words", words)
        if a_off:
            protection = protection + [dict(w, s=w["s"] + a_off, e=w["e"] + a_off) for w in protection]
        auto_keep = dead_air_keep(src, protection, start, end, int(clip.get("audio_stream_index") or 0), a_off,
                                  clip.get("sfx") or [])
    keep = auto_keep
    if clip.get("auto_edit"):
        legacy_keep = tight_cut(src, words, start, end)
        if legacy_keep:
            keep = [(max(a, c), min(b, d)) for a, b in keep for c, d in legacy_keep
                    if max(a, c) < min(b, d)] if keep else legacy_keep
    cuts = _clip_cuts(clip)
    if cuts:                          # parts deleted in the editor: the rest is joined up
        keep = _minus(keep or [(0.0, end - start)], cuts) or None
    if auto_keep and keep:
        fps = config.OUT_FPS
        keep = [(round(a * fps) / fps, end - start if abs(b - (end - start)) < .001 else round(b * fps) / fps)
                for a, b in keep]
    hook_a, hook_b = (start, end) if overlay == "hook" else clip.get("hook_span") or [start, min(end, start + 4)]
    hook_span = (_out_t(max(0, hook_a - start), keep), _out_t(min(end - start, hook_b - start), keep))
    hook = "" if overlay == "captions" else clip.get("hook", "")
    captions = [] if overlay == "hook" or clip.get("no_caps") else words
    _cv.hook_emojis = []
    if keep:
        # captions are written for the tightened clip, which starts at 0 and is as long as what was kept
        ass = build_ass(_retime(captions, keep, start, drop_fillers=not auto_keep or bool(clip.get("auto_edit"))),
                        0.0, sum(b - a for a, b in keep), hook,
                        clip.get("style", "bounce"), clip.get("highlight_words", []), hook_y, cap_y, hook_span)
    else:
        ass = build_ass(captions, start, end, hook, clip.get("style", "bounce"),
                        clip.get("highlight_words", []), hook_y, cap_y, hook_span)
    ass = _extra_text(ass, clip.get("texts"), start, end, keep)
    mark = _clean(clip.get("watermark", "Edited Automatically") or "")[:40]
    if mark:
        cw, ch = _wh()
        pos = clip.get("watermark_pos")
        anchor = (f"5\\pos({round(pos[0] * cw)},{round(pos[1] * ch)})" if pos
                  else f"9\\pos({cw - round(44 * cw / 1080)},{round(56 * cw / 1080)})")
        long = sum(b - a for a, b in keep) if keep else end - start
        ass += (f"Dialogue: 5,{_ts(0)},{_ts(long)},Cap,,0,0,0,,{{\\an{anchor}\\fs{round(46 * cw / 1080)}\\bord3"
                f"\\alpha&H40&\\c&HFFFFFF&}}{mark}\n")
    # The clip is made in a short work folder and moved into place afterwards. The caption step of ffmpeg cannot
    # open files whose full path is longer than 260 characters on Windows, which a data folder deep inside other
    # folders easily reaches. Inside the work folder every name is short and relative (a drive colon would also
    # break the filter syntax).
    work = Path(tempfile.mkdtemp(prefix="pk"))
    try:
        fx = {k: clip.get(k) for k in ("look", "zoom", "sharpen") + FX_FLAGS}
        s = min(3.0, max(1.0, float(clip.get("fscale") or 1.0)))
        cx = clip.get("crop_x", 0.5)
        if clip.get("track") and clip.get("track_keys") and layout not in ("cam", "facegame"):
            cw = _wh()[0]
            wf = cw / max(cw * s, fg_h * s * src_w / max(1, src_h))     # how much of the picture's width the box shows
            cx = _follow(_retime_keys(clip["track_keys"], keep), wf)
        hook_emo = [(e, px, xy, hook_span[0], hook_span[1])
                    for e, px, xy in (getattr(_cv, "hook_emojis", None) or [])]
        cam_keys_rel = []
        if cam and clip.get("cam_keys"):
            for ck in clip["cam_keys"]:
                if not isinstance(ck, dict):
                    continue
                px_box = cam_px(ck.get("box"), src_w, src_h)
                try:
                    t_val = float(ck.get("t", 0.0))
                except (TypeError, ValueError):
                    continue
                if px_box:
                    cam_keys_rel.append([max(0.0, t_val - start), px_box])
            cam_keys_rel.sort(key=lambda x: x[0])
            if keep and cam_keys_rel:
                cam_keys_rel = _retime_cam_keys(cam_keys_rel, keep)
        frame = {"cx": cx, "cy": clip.get("crop_y", 0.5), "s": s, "band_y": clip.get("band_y", 0.5),
                 "band_s": clip.get("band_s", 1.0), "band_x": clip.get("band_x", 0.5), "gp": gp, "cam": cam,
                 "cam_keys": cam_keys_rel, "src_w": src_w, "src_h": src_h, "audio_offset": a_off,
                 "audio_stream_index": int(clip.get("audio_stream_index") or 0),
                 "auto_keep": bool(auto_keep),
                 "mute_audio": bool(clip.get("mute_audio")),
                 "masks": _mask_parts(clip, src_w, src_h, keep), "outro": clip.get("outro"),
                 "emojis": hook_emo + _cv.caption_emojis + _emoji_parts(clip, keep), "sfx": _sfx_parts(clip, keep),
                 "duck_sfx": clip.get("duck_sfx"), "stickers": _sticker_parts(clip, keep),
                 "impacts": [dict(impact, t=round(_out_t(impact["t"] - start, keep) * config.OUT_FPS) / config.OUT_FPS)
                             for impact in clip.get("impacts", []) if start <= impact["t"] < end
                             and not any(a <= impact["t"] - start < b for a, b in cuts)]}
        for key in ("bleeps", "cold_open"):
            spans = [clip[key]] if key == "cold_open" and clip.get(key) else clip.get(key) or []
            frame[key] = [(_out_t(max(0, a - start), keep), _out_t(min(end - start, b - start), keep))
                          for a, b in spans if start < b and a < end]
        out = _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, keep, frame, fx)
        out["auto_cut"] = round((end - start) - sum(b - a for a, b in keep), 1) if keep else 0
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _sticker_parts(clip, keep):
    if not clip.get("meme_dir"):
        return []
    cw, ch = _wh()
    start, end = clip["start"], clip["end"]
    result = []
    for item in clip.get("texts") or []:
        name = item.get("sticker", "")
        if not re.fullmatch(r"meme-[a-f0-9]{32}\.png", name):
            continue
        path = Path(clip["meme_dir"]) / name
        a = _out_t(max(0,item.get("a",start)-start),keep)
        b = _out_t(min(end-start,item.get("b",end)-start),keep)
        if path.is_file() and b > a + .01:
            result.append((path, int(item["size"]*cw/1080), int(ch*.56), item["x"]*cw, item["y"]*ch, a,b))
    return result


FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mhm", "eh"}


def _silences(src, start, end, floor="-33dB", least=0.4, stream_index=None) -> list:
    """Quiet stretches of a clip as heard, [(from, to), ...] in seconds from the clip's start."""
    mapping = ["-map", f"0:a:{stream_index}"] if stream_index is not None else []
    p = subprocess.run(["ffmpeg", "-v", "info", "-nostats", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
                        *mapping, "-vn", "-af", f"silencedetect=noise={floor}:d={least}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=600)
    if p.returncode and stream_index is not None:
        raise RuntimeError("Could not analyze the selected audio track for Auto Cut.")
    out, at = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", p.stderr):
        if m.group(1) == "start":
            at = max(0.0, float(m.group(2)))
        elif at is not None:
            out.append((at, float(m.group(2))))
            at = None
    if at is not None:
        out.append((at, end - start))
    return out


def dead_air_keep(src, words, start, end, stream_index, audio_offset, sounds):
    """Remove only the middle of long, quiet interior gaps, on output frame boundaries."""
    duration = end - start
    audio_start = max(0.0, start - audio_offset)
    audio_end = end - audio_offset
    if audio_end <= audio_start:
        return None
    shift = audio_start + audio_offset - start
    fps = config.OUT_FPS
    protected = [(w["s"] - start - .15, w["e"] - start + .15) for w in words]
    # An editor-added sound stays anchored to its source moment on subsequent exports.
    protected.extend((s["t"] - start - .4, s["t"] - start + .4) for s in sounds)
    pauses = _silences(src, audio_start, audio_end, floor="-50dB", least=2.0,
                       stream_index=stream_index)
    cuts = []
    for quiet_start, quiet_end in pauses:
        left, right = quiet_start + shift, quiet_end + shift
        if quiet_start <= .03 or right >= duration - .03 or quiet_end >= audio_end - audio_start - .03:
            continue
        preceding = max((w for w in words if w["e"] <= start + left + .15),
                        key=lambda w: w["e"], default=None)
        # A question or trailing thought often sets up the next line.
        if preceding and str(preceding["w"]).rstrip().endswith(("?", "؟", "…", "...", "—")) and right - left < 3.5:
            continue
        for a, b in _minus([(left + .32, right - .42)], sorted(protected)):
            a, b = math.ceil(a * fps) / fps, math.floor(b * fps) / fps
            if b - a >= .65:
                cuts.append((a, b))
    return _minus([(0.0, duration)], cuts) if cuts else None


def tight_cut(src, words, start, end, breath=0.13):
    """Auto edit: the stretches of a clip worth keeping, as [(from, to), ...] in seconds from the clip's start.
    Dead air (found by listening, since word timings run into each other) and filler words go.
    None when there is nothing worth cutting."""
    dur = end - start
    cuts = [(a + breath, b - breath) for a, b in _silences(src, start, end) if b - a > 2 * breath + 0.15]
    for w in words:                                            # um, uh, er: the word itself
        if start <= w["s"] < end and _key(w["w"]) in FILLERS:
            cuts.append((w["s"] - start, min(w["e"], w["s"] + 0.5) - start))
    keep, at = [], 0.0
    for a, b in sorted(cuts):
        if a > at:
            keep.append((at, a))
        at = max(at, b)
    if at < dur:
        keep.append((at, dur))
    keep = [(round(a, 3), round(b, 3)) for a, b in keep if b - a >= 0.15]
    kept = sum(b - a for a, b in keep)
    if not keep or dur - kept < 0.4 or kept < 3:
        return None
    return keep


def _retime(words, keep, start, drop_fillers=True):
    """The words of a clip moved onto the tightened timeline. Words inside a removed stretch are dropped."""
    out, at = [], 0.0
    for a, b in keep:
        for w in words:
            s = w["s"] - start
            if a - 0.05 <= s < b and (not drop_fillers or _key(w["w"]) not in FILLERS):
                e = min(w["e"] - start, b)
                out.append(dict(w, s=at + max(0.0, s - a), e=at + min(b - a, max(s - a + 0.08, e - a))))
        at += b - a
    return out


# ---------------------------------------------------------------- effects
# A look is a colour grade. Each is a short ffmpeg chain; the editor previews it with the closest CSS filter.
LOOKS = {"none": "",
         "sdr": "eq=contrast=1.10:saturation=1.08:brightness=0.025",
         "cinematic": "eq=contrast=1.12:saturation=1.08:brightness=-0.02,colorbalance=rs=-0.05:bs=0.07:rh=0.07:bh=-0.06",
         "vivid": "eq=contrast=1.08:saturation=1.4",
         "warm": "colorbalance=rm=0.08:bm=-0.08:rh=0.04:bh=-0.04,eq=saturation=1.1",
         "cool": "colorbalance=rm=-0.06:bm=0.09:rh=-0.03:bh=0.05",
         "mono": "hue=s=0,eq=contrast=1.12",
         "vintage": "eq=contrast=0.92:saturation=0.8:brightness=0.03,colorbalance=rs=0.08:bs=-0.05:rm=0.04"}
ZOOMS = ("none", "slow", "punch")
ZOOM_BY = 0.12          # how far a zoom goes in: 12%
PUNCH_EVERY = 3.0       # "punch" cuts between normal and zoomed-in this often, in seconds
FX_FLAGS = ("vignette", "grain", "fade", "bar")


def _fx_chain(fx: dict, dur: float) -> str:
    """The picture effects, applied to the framed video before the captions go on (so text never zooms or tints)."""
    W, H = _wh()
    fps, frames = config.OUT_FPS, max(1, int(dur * config.OUT_FPS))
    out = []
    z = {"slow": f"1+{ZOOM_BY}*on/{frames}",
         "punch": f"1+{ZOOM_BY}*gt(mod(on,{int(PUNCH_EVERY * 2 * fps)}),{int(PUNCH_EVERY * fps) - 1})"}.get(fx.get("zoom"))
    if z:
        # worked out at twice the size, so the slow move is smooth instead of stepping a pixel at a time
        up = f"scale={W * 2}:{H * 2}:flags=bilinear," if fx.get("zoom") == "slow" else ""     # a cut needs no smoothing
        out.append(f"{up}zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")
    if LOOKS.get(fx.get("look")):
        out.append(LOOKS[fx["look"]])
    if fx.get("look") == "sdr":
        amount = fx.get("sharpen")
        amount = 0.85 if amount is None else amount
        if amount > 0:
            out.append(f"unsharp=5:5:{amount:.3f}:5:5:0")
        # White-to-black overlay at 14%, applied to luma so the gradient does not tint the picture.
        out.append("geq=lum='st(0,lum(X,Y));ld(0)+0.14*(1-2*Y/(H-1))*max(0,min(ld(0)-16,235-ld(0)))'"
                   ":cb='cb(X,Y)':cr='cr(X,Y)':interpolation=nearest")
    if fx.get("vignette"):
        out.append("vignette=angle=PI/4.6")
    if fx.get("grain"):
        out.append("noise=alls=9:allf=t")
    return ",".join(out)


def _add_outro(work: Path, name: str, outro: Path):
    """The outro joined to the end of the clip: fitted to the clip's shape (black bars if its shape differs),
    same frame rate and sound format. An outro with no sound gets silence."""
    W, H = _wh()
    has_audio = '"codec_type": "audio"' in run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(outro)])
    a_in = ["-i", str(outro)] if has_audio else ["-f", "lavfi", "-t", "15", "-i", "anullsrc=r=48000:cl=stereo"]
    oa = "[1:a]" if has_audio else "[2:a]"
    # the outro's own length decides how long its sound runs (padded with silence, never longer)
    olen = min(15.5, float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(outro)]).strip() or 0))
    fc = (f"[0:v]fps={config.OUT_FPS},setsar=1,setpts=PTS-STARTPTS,format=yuv420p[mv];"
          f"[0:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[ma];"
          f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,fps={config.OUT_FPS},setsar=1,setpts=PTS-STARTPTS,format=yuv420p[ov];"
          f"{oa}aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-14:TP=-1.5,apad,atrim=0:{olen:.3f},asetpts=PTS-STARTPTS[oa];"
          f"[mv][ma][ov][oa]concat=n=2:v=1:a=1[v][a]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", f"{name}.mp4", "-t", "15.5", "-i", str(outro)]
    if not has_audio:
        cmd += a_in
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", f"{name}_o.mp4"]
    run(cmd, cwd=work, timeout=900)
    (work / f"{name}_o.mp4").replace(work / f"{name}.mp4")


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, keep=None, frame=None, fx=None) -> dict:
    global _qsv_ok
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*"):
        if f.suffix.lower() in (".ttf", ".otf"):
            shutil.copyfile(f, work / "f" / f.name)
    fx = fx or {}
    dur = sum(b - a for a, b in keep) if keep else end - start
    W, H = _wh()
    frame = frame or {}
    a_off = max(-30.0, min(30.0, float(frame.get("audio_offset") or 0.0)))
    clip_dur = max(0.1, float(end) - float(start))
    a_start = float(start) - a_off
    earliest = min(float(start), max(0.0, a_start))
    pre = max(0.0, earliest - 6.0) if earliest > 6.5 else 0.0
    v_post = max(0.0, float(start) - pre)
    a_post = a_start - pre
    read_dur = max(v_post, max(0.0, a_post)) + clip_dur + 1.0
    sel = "+".join(f"between(t,{a},{b})" for a, b in keep) if keep else None
    if keep and frame.get("auto_keep"):
        # Select whole output frames in both streams. Audio packets below are exactly one video frame long.
        fps = config.OUT_FPS
        sel = "+".join(f"gte(n,{math.ceil(a * fps - 1e-7)})*lt(n,{math.ceil(b * fps - 1e-7)})"
                       for a, b in keep)
    if frame.get("mute_audio"):
        a_head = f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS"
    else:
        a_idx = max(0, int(frame.get("audio_stream_index") or 0))
        if a_post >= 0.0:
            a_core = (f"[0:a:{a_idx}]atrim=start={a_post:.3f}:duration={clip_dur:.3f},asetpts=PTS-{a_post:.3f}/TB,"
                      f"aresample=48000:async=1000:first_pts=0,aformat=channel_layouts=stereo,"
                      f"apad,atrim=0:{clip_dur:.3f},asetpts=PTS-STARTPTS")
        else:
            pad_ms = int(round((-a_post) * 1000))
            a_avail = max(0.05, clip_dur + a_post)
            a_core = (f"[0:a:{a_idx}]atrim=start=0.000:duration={a_avail:.3f},"
                      f"aresample=48000:async=1000:first_pts=0,asetpts=PTS-STARTPTS,aformat=channel_layouts=stereo,"
                      f"adelay={pad_ms}|{pad_ms},apad,atrim=0:{clip_dur:.3f},asetpts=PTS-STARTPTS")
        if sel and frame.get("auto_keep"):
            a_core += f",asetnsamples=n={48000 // config.OUT_FPS}:p=0"
        a_head = f"{a_core},aselect='{sel}',asetpts=N/SR/TB" if sel else a_core
    if sel:
        v_head = (f"[0:v]trim=start={v_post:.3f}:duration={clip_dur:.3f},setpts=PTS-{v_post:.3f}/TB,"
                  f"fps={config.OUT_FPS}:start_time=0,select='{sel}',setpts=N/{config.OUT_FPS}/TB")
    else:
        v_head = f"[0:v]trim=start={v_post:.3f}:duration={clip_dur:.3f},setpts=PTS-{v_post:.3f}/TB,fps={config.OUT_FPS}:start_time=0"

    masks = frame.get("masks") or []
    if masks:
        fc = f"{v_head},split={1 + len(masks)}[vmain]" + "".join(f"[vms{n}]" for n in range(len(masks))) + ";"
    else:
        fc = f"{v_head}[vmain];"

    fc += _filter(layout, fg_h, frame.get("cx", 0.5), frame.get("cy", 0.5), frame.get("s", 1.0), frame.get("cam"),
                  frame.get("band_y", 0.5), frame.get("band_s", 1.0), frame.get("band_x", 0.5),
                  v_in="[vmain]", cam_keys=frame.get("cam_keys"),
                  src_w=frame.get("src_w", 0), src_h=frame.get("src_h", 0), dur=dur)
    cur = "[v0]"
    pre_fx = _fx_chain(fx, dur)
    if pre_fx:
        fc += f"{cur}{pre_fx}[v1];"
        cur = "[v1]"
    for n, (ta, tb, m) in enumerate(masks):
        # each cut-out: its own copy of the picture, cut, shaped and laid over the frame while its part plays, under the text
        _mask_pngs(work, m, n)
        (cx_, cy_, cw_, ch_), (mw, mh), (ox, oy) = m["cut"], m["size"], m["at"]
        on = f":enable='between(t,{ta},{tb})'"
        fc += (f"[vms{n}]crop={cw_}:{ch_}:{cx_}:{cy_},scale={mw}:{mh},setsar=1,format=yuv420p[mraw{n}];"
               f"movie=mk{n}.png,format=gray,loop=loop=-1:size=1,setpts=N/{config.OUT_FPS}/TB[mka{n}];[mraw{n}][mka{n}]alphamerge[mk{n}];"
               f"{cur}[mk{n}]overlay={ox}:{oy}:shortest=1{on}[vm{n}];")
        cur = f"[vm{n}]"
        if m["border"]:
            fc += f"movie=rg{n}.png[rg{n}];{cur}[rg{n}]overlay={ox}:{oy}{on}[vr{n}];"
            cur = f"[vr{n}]"
    impacts = frame.get("impacts") or []
    if impacts:
        def pulse(clock, p):
            elapsed = f"({clock}-{p['t']:.9f})"
            length = p["length"]
            if p["kind"] == "impact":
                peak = min(4 / config.OUT_FPS, length / 2)
                rise = f"clip({elapsed}/{peak:.9f},0,1)"
                tail = f"clip(({length:.9f}-{elapsed})/{length-peak:.9f},0,1)"
                curve = f"if(lt({elapsed},{peak:.9f}),1-pow(1-{rise},3),pow({tail},2.25))"
            else:
                curve = f"clip(1-{elapsed}/{length:.9f},0,1)"
            return f"{p['strength']:.4f}*between({elapsed},0,{length:.9f})*({curve})"
        clock = f"on/{config.OUT_FPS}"
        zoom = "+".join(f"{.25 if p['kind'] == 'impact' else .24}*{pulse(clock,p)}" for p in impacts)
        soft_shake = all(p["kind"] == "impact" for p in impacts)
        x = f"(iw-iw/zoom)*(0.5+0.22*sin(on*{.93 if soft_shake else 1.8}))"
        y = f"(ih-ih/zoom)*(0.5+0.14*sin(on*{1.17 if soft_shake else 2.1}))"
        exposure = ""
        hits = [p for p in impacts if p["kind"] == "impact"]
        if hits:
            fps = config.OUT_FPS
            frames = sorted({n for p in hits for n in range(round(p["t"] * fps), math.ceil((p["t"] + p["length"]) * fps) + 1)})
            def amount(t, p):
                dt, length = t - p["t"], p["length"]
                peak = min(4 / fps, length / 2)
                if not 0 < dt < length:
                    return 0.0
                if dt < peak:
                    ratio = dt / peak
                    return p["strength"] * (1 - (1 - ratio) ** 3)
                return p["strength"] * ((length - dt) / (length - peak)) ** 2.25
            commands = [f"{max(0,n/fps-.00001):.8f} exposure exposure {0.32 * min(1,sum(amount(n/fps,p) for p in hits)) ** 2:.6f};" for n in frames]
            (work / "impact-exposure.txt").write_text("\n".join(commands), encoding="utf-8")
            exposure = "sendcmd=f=impact-exposure.txt,exposure=exposure=0,"
        trail = "+".join(f"between(t,{p['t']+.5/config.OUT_FPS:.6f},{p['t']+min(.30,p['length']*.44):.6f})"
                         for p in hits)
        motion_blur = f"tmix=frames=3:weights='1 2 5':enable='{trail}'," if trail else ""
        blur = "+".join(f"between(t,{p['t']:.6f},{p['t']+min(.24,p['length']*.32):.6f})"
                        for p in impacts if p["kind"] == "whip") or "0"
        fc += (f"{cur}zoompan=z='1+min(.25,{zoom})':x='{x}':y='{y}':d=1:s={W}x{H}:fps={config.OUT_FPS},"
               f"{motion_blur}{exposure}gblur=sigma=3:enable='{blur}'[impact];")
        cur = "[impact]"
    fc += f"{cur}ass={name}.ass:fontsdir=f[v2]"
    cur = "[v2]"
    for n, (emo, px, (ex, ey), ta, tb) in enumerate(frame.get("emojis") or []):
        # each emoji over everything else, only between its moments, with a quick pop as it appears
        w_, h_ = _emoji_png(work / f"em{n}.png", emo, px)
        pop = f"if(lt(t-{ta},0.12),0.6+0.4*(t-{ta})/0.12,1)"
        fc += (f";movie=em{n}.png,loop=loop=-1:size=1,setpts=N/{config.OUT_FPS}/TB,format=rgba,"
               f"scale=w='ceil({w_}*{pop}/2)*2':h='ceil({h_}*{pop}/2)*2':eval=frame[em{n}];"
               f"{cur}[em{n}]overlay=x='{ex:.0f}-w/2':y='{ey:.0f}-h/2':shortest=1:enable='between(t,{ta},{tb})'[ve{n}]")
        cur = f"[ve{n}]"
    for n, (path, width, height, x, y, ta, tb) in enumerate(frame.get("stickers") or []):
        from PIL import Image
        with Image.open(path) as image:
            ratio = min(width/image.width,height/image.height)
            target = (max(2,round(image.width*ratio/2)*2),max(2,round(image.height*ratio/2)*2))
            image.convert("RGBA").resize(target,Image.Resampling.LANCZOS).save(work / f"reaction{n}.png")
        fc += (f";movie=reaction{n}.png,loop=loop=-1:size=1,setpts=N/{config.OUT_FPS}/TB,format=rgba[reaction{n}];"
               f"{cur}[reaction{n}]overlay=x='{x:.0f}-w/2':y='{y:.0f}-h/2':shortest=1:enable='between(t,{ta},{tb})'[vrx{n}]")
        cur = f"[vrx{n}]"
    if fx.get("bar"):       # a thin bar along the bottom that fills as the clip plays
        fc += (f";color=c=0xE11D2E:s={W}x12:r={config.OUT_FPS}:d={dur:.2f}[bar];"
               f"{cur}[bar]overlay=x='-w+w*t/{dur:.2f}':y=H-12:shortest=1[v3]")
        cur = "[v3]"
    if fx.get("fade"):
        fc += f";{cur}fade=t=in:st=0:d=0.35,fade=t=out:st={max(0.0, dur - 0.45):.2f}:d=0.45[v]"
    else:
        fc += f";{cur}null[v]"
    af = "aresample=48000,aformat=channel_layouts=stereo,alimiter=limit=0.95:level=disabled"
    if fx.get("fade"):
        af += f",afade=t=in:st=0:d=0.2,afade=t=out:st={max(0.0, dur - 0.45):.2f}:d=0.45"
    fc += f";{a_head}[araw]"
    bleeps = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in frame.get("bleeps", []) if b > a)
    if bleeps:
        fc += (f";[araw]volume=0:enable='{bleeps}'[censored]"
               f";aevalsrc='if({bleeps},0.18*sin(2*PI*1000*t),0)':s=48000:d={dur:.3f}[beep]"
               ";[censored][beep]amix=inputs=2:duration=first:normalize=0[asrc]")
    else:
        fc += ";[araw]anull[asrc]"
    under = (["-stream_loop", "-1", "-ss", f"{frame['gp'][1]:.2f}", "-i", str(frame["gp"][0])]
             if layout == "split" and frame.get("gp") else [])
    sounds, extra = frame.get("sfx") or [], []
    if sounds:
        # meme sounds: each trimmed to 4 s, set to its volume, delayed to its moment, mixed under the voice
        first = 1 + (1 if under else 0)
        afc, labels = f";[asrc]{af}[a0]", ""
        for n, (sf, st, db) in enumerate(sounds):
            extra += ["-i", sf]
            ms = int(st * 1000)
            afc += (f";[{first + n}:a]atrim=0:4,afade=t=out:st=3.3:d=0.7,aresample=48000,aformat=channel_layouts=stereo,"
                    f"volume={db}dB,adelay={ms}|{ms}[s{n}]")
            labels += f"[s{n}]"
        if frame.get("duck_sfx"):
            afc += (";[a0]asplit[voice][side]"
                    f";{labels}amix=inputs={len(sounds)}:normalize=0:duration=longest[soundbed]"
                    ";[soundbed][side]sidechaincompress=threshold=0.035:ratio=7:attack=15:release=220[ducked]"
                    ";[voice][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[aout]")
        else:
            afc += f";[a0]{labels}amix=inputs={len(sounds) + 1}:normalize=0:duration=first:dropout_transition=0,alimiter=limit=0.97[aout]"
        fc += afc
    else:
        fc += f";[asrc]{af}[aout]"
    amap = ["-map", "[aout]"]
    base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{pre:.3f}", "-t", f"{read_dur:.3f}", "-i", str(src)] + under + extra + [
            "-filter_complex", fc, "-map", "[v]"] + amap + [
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", "-pix_fmt"]
    hw = _hw_encoder()
    encoders = ([(hw, HW[hw])] if hw else []) + [("x264", ["yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "veryfast"])]
    err = None
    for label, enc in encoders:
        p = subprocess.run(base + enc + [f"{name}.mp4"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=1800)
        if p.returncode == 0:
            break
        err = p.stderr[-1200:]
        if label != "x264":
            _qsv_ok = False            # this card failed on a real clip: the processor does the rest
    else:
        raise RuntimeError(f"Render failed: {err}")
    for a, b in frame.get("cold_open", []):
        if b - a < .15:
            continue
        original = f"{name}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-i", original, "-filter_complex",
             f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v0];"
             f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a0];"
             "[v0][a0][0:v][0:a]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-movflags", "+faststart", "cold-open.mp4"], cwd=work)
        (work / "cold-open.mp4").replace(work / original)
    if frame.get("outro") and Path(frame["outro"]).is_file():
        _add_outro(work, name, Path(frame["outro"]))
    export_duration = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 f"{name}.mp4"], cwd=work).strip())
    cover_t = min(1.2, export_duration / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{name}.{ext}"), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout, "export_duration": round(export_duration, 3)}
