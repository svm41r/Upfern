"""Local U²-Net cutouts and optional withoutBG cloud cutouts."""
import hashlib
from io import BytesIO
from threading import Lock

import cv2
import httpx
import numpy as np
from PIL import Image

from . import ai, config

_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
_MODEL_SHA = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
_LOCK = Lock()
_net = None


def _network():
    global _net
    if _net is not None:
        return _net
    path = config.DATA_DIR / "models" / "u2netp.onnx"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != _MODEL_SHA:
        response = httpx.get(_MODEL_URL, follow_redirects=True, timeout=35)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != _MODEL_SHA:
            raise ValueError("The downloaded cutout model failed verification.")
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix(".tmp")
        staged.write_bytes(response.content)
        staged.replace(path)
    _net = cv2.dnn.readNetFromONNX(str(path))
    return _net


def local(frame):
    with Image.open(BytesIO(frame)) as source:
        image = source.convert("RGB")
    rgb = np.asarray(image.resize((320, 320), Image.Resampling.LANCZOS), dtype=np.float32)
    rgb /= max(1.0, float(rgb.max()))
    rgb = (rgb - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
    batch = np.ascontiguousarray(rgb.transpose((2, 0, 1))[None, ...])
    with _LOCK:
        net = _network()
        net.setInput(batch)
        matte = net.forward("1959")[0, 0]
    low, high = float(matte.min()), float(matte.max())
    if not np.isfinite(matte).all() or high - low < .001:
        raise ValueError("No clear foreground was detected.")
    matte = np.clip(((matte - low) / (high - low) - .06) / .88, 0, 1)
    alpha = Image.fromarray(np.uint8(matte * 255)).resize(image.size, Image.Resampling.LANCZOS)
    image.putalpha(alpha)
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def cloud_key():
    return ai.keys().get("cover_withoutbg") or config.env("WITHOUTBG_API_KEY", "")


def check_cloud_key(key):
    httpx.get("https://api.withoutbg.com/available-credit", headers={"X-API-Key": key}, timeout=12).raise_for_status()


def cloud(frame, key):
    result = httpx.post("https://api.withoutbg.com/v1.0/image-without-background",
                        headers={"X-API-Key": key}, files={"file": ("cover.jpg", frame, "image/jpeg")}, timeout=50)
    result.raise_for_status()
    with Image.open(BytesIO(result.content), formats=("PNG",)) as image:
        if image.mode != "RGBA":
            raise ValueError("The provider did not return a transparent cutout.")
        image.verify()
    return result.content
