"""Generate an image with the agent's API and return the raw bytes.

OpenRouter uses POST {base}/images. OpenAI uses POST {base}/images/generations.
The chat model is left unchanged; a separate image model draws the picture.
"""
import base64

import requests
from django.conf import settings

ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
MEDIA_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
# OpenAI's generations endpoint wants pixel sizes, not an aspect ratio.
_OPENAI_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "3:2": "1536x1024",
    "4:3": "1536x1024",
    "9:16": "1024x1536",
    "2:3": "1024x1536",
    "3:4": "1024x1536",
}


class ImageError(Exception):
    """The image service refused the request or returned nothing usable."""


def model_for(agent):
    """The image model. A blank image model uses the agent's chat model."""
    chosen = (getattr(agent, "image_model", "") or "").strip()
    if chosen:
        return chosen
    chat = (getattr(agent, "model", "") or "").strip()
    if not chat:
        raise ImageError("This agent has no model set for image generation.")
    return chat


def endpoint_for(base_url):
    """Return (url, kind) where kind is 'openai' or 'openrouter'."""
    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    if "api.openai.com" in base:
        if base.endswith("/images/generations"):
            return base, "openai"
        return base + "/images/generations", "openai"
    if base.endswith("/images"):
        return base, "openrouter"
    return base + "/images", "openrouter"


def generate(agent, prompt, aspect_ratio="1:1"):
    """Return (bytes, media_type, model). Raises ImageError."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageError("Describe the image to generate.")
    if len(prompt) > 4000:
        raise ImageError("The image description is too long (4000 characters).")
    if getattr(agent, "provider", "") != "openai":
        raise ImageError("Image generation needs an OpenAI-compatible agent, such as OpenRouter or OpenAI.")
    if not getattr(agent, "api_key", ""):
        raise ImageError("This agent has no API key, so it cannot generate an image.")
    ratio = (aspect_ratio or "1:1").strip()
    if ratio not in ASPECTS:
        ratio = "1:1"
    model = model_for(agent)
    url, kind = endpoint_for(getattr(agent, "base_url", ""))
    headers = {"Authorization": f"Bearer {agent.api_key}", "Content-Type": "application/json"}
    extra = agent.headers() if hasattr(agent, "headers") else {}
    headers.update(extra)
    if kind == "openai":
        payload = {"model": model, "prompt": prompt, "size": _OPENAI_SIZE[ratio], "n": 1,
                   "response_format": "b64_json"}
    else:
        payload = {"model": model, "prompt": prompt, "aspect_ratio": ratio, "output_format": "png"}
    timeout = getattr(settings, "PSEDITOR_IMAGE_TIMEOUT", 180)
    data = _post(url, headers, payload, timeout, kind)
    images = data.get("data") if isinstance(data, dict) else None
    if not images or not isinstance(images, list):
        raise ImageError("The image service returned no image.")
    first = images[0] if isinstance(images[0], dict) else {}
    raw = first.get("b64_json") or ""
    if isinstance(raw, str) and raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        blob = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        blob = b""
    if not blob:
        raise ImageError("The image service returned no image data.")
    limit = getattr(settings, "PSEDITOR_MAX_IMAGE_BYTES", 12 * 1024 * 1024)
    if len(blob) > limit:
        raise ImageError("The generated image is too large to save.")
    media = str(first.get("media_type") or "image/png").split(";")[0].strip().lower()
    if media not in MEDIA_EXT:
        media = _sniff(blob)
    return blob, media, model


def slug(prompt):
    words = []
    for word in (prompt or "").replace("_", " ").split():
        clean = "".join(ch for ch in word if ch.isalnum())
        if clean:
            words.append(clean.lower())
        if len(words) >= 6:
            break
    text = "-".join(words)[:40].strip("-")
    return text or "image"


def _sniff(blob):
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob.startswith(b"GIF8"):
        return "image/gif"
    if blob.lstrip()[:4] in (b"<svg", b"<?xml"):
        return "image/svg+xml"
    return "image/png"


def _post(url, headers, payload, timeout, kind):
    resp = _send(url, headers, payload, timeout)
    if resp.status_code == 400:
        retry = dict(payload)
        message = _error_message(resp).lower()
        if kind == "openrouter":
            if "output_format" in message:
                retry.pop("output_format", None)
            if "aspect_ratio" in message:
                retry.pop("aspect_ratio", None)
        elif "response_format" in message:
            retry.pop("response_format", None)
        if retry != payload:
            resp = _send(url, headers, retry, timeout)
    if resp.status_code >= 400:
        raise ImageError(_error_message(resp) or f"Image service returned {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError:
        raise ImageError("Image service returned a response that was not JSON.")
    if not isinstance(data, dict):
        raise ImageError("Image service returned an unexpected response.")
    return data


def _send(url, headers, payload, timeout):
    try:
        return requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ImageError(f"Could not reach the image service: {exc}")


def _error_message(resp):
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")
    if isinstance(err, str):
        return err
    return str(data.get("message") or "")
