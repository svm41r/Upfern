"""Image-to-image cover generation with the saved Gemini or OpenAI API credential."""
import base64
from io import BytesIO

import httpx
from PIL import Image, ImageOps

from . import ai, config

MODELS = {"gemini": "gemini-nano-banana-2.1", "openai": "chatgpt-image-latest"}
NAMES = {"gemini": "Gemini", "openai": "ChatGPT"}


def provider_key(provider):
    keys = ai.keys()
    saved = keys.get("cover_" + provider) or keys.get(provider)
    if saved:
        return saved
    current = ai.current()
    return (current["key"] if current["provider"] == provider else "") or config.env(
        "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY", "")


def headers(provider, key):
    if provider == "gemini":
        return {"x-goog-api-key": key, "Api-Revision": "2026-05-20"}
    return {"Authorization": f"Bearer {key}"}


def check_key(provider, key):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/" if provider == "gemini"
           else "https://api.openai.com/v1/models/")
    httpx.get(url + MODELS[provider], headers=headers(provider, key), timeout=12).raise_for_status()


def image_bytes(data, output=False):
    raw = base64.b64decode(data, validate=True)
    with Image.open(BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as image:
        if image.width * image.height > 24_000_000:
            raise ValueError("Image dimensions are too large.")
        image = ImageOps.exif_transpose(image).convert("RGB")
        if output:
            image = ImageOps.fit(image, (1080, 1920), Image.Resampling.LANCZOS)
        else:
            image.thumbnail((1080, 1920), Image.Resampling.LANCZOS)
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=94)
    return buf.getvalue()


def generate(provider, key, frame, prompt):
    opts = {"headers": headers(provider, key), "timeout": httpx.Timeout(180, connect=12)}
    if provider == "gemini":
        res = httpx.post("https://generativelanguage.googleapis.com/v1beta/interactions", **opts, json={
            "model": MODELS[provider], "store": False,
            "input": [{"type": "text", "text": prompt},
                      {"type": "image", "mime_type": "image/jpeg", "data": base64.b64encode(frame).decode("ascii")}],
            "response_format": {"type": "image", "mime_type": "image/jpeg", "aspect_ratio": "9:16", "image_size": "2K"},
        })
        res.raise_for_status()
        result = res.json()
        blocks = [content for step in result.get("steps", []) if step.get("type") == "model_output"
                  for content in step.get("content", [])]
        blocks.extend(result.get("outputs") or [])
        generated = next((part["data"] for part in reversed(blocks) if part.get("type") == "image" and part.get("data")), "")
    else:
        res = httpx.post("https://api.openai.com/v1/images/edits", **opts,
                         files={"image": ("cover.jpg", frame, "image/jpeg")},
                         data={"model": MODELS[provider], "prompt": prompt, "size": "1024x1536",
                               "quality": "high", "output_format": "jpeg"})
        res.raise_for_status()
        generated = next((part["b64_json"] for part in res.json().get("data", []) if part.get("b64_json")), "")
    if not generated:
        raise ValueError("The provider returned no image.")
    return image_bytes(generated, output=True)
