"""Generate a short video clip with the agent's API and return the raw bytes.

OpenRouter and OpenAI both use an async job: POST {base}/videos, poll until
the job finishes, then download the mp4. Clips are capped at 8 seconds.
"""
import base64
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings

from .imagegen import slug

ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3")
SHORT = (4, 5, 6, 8)
DEFAULT_DURATION = 4
DEFAULT_OPENROUTER = "google/veo-3.1"
DEFAULT_OPENAI = "sora-2"
MEDIA_EXT = {"video/mp4": ".mp4", "video/webm": ".webm"}
_OPENAI_SIZE = {
    "16:9": "1280x720",
    "4:3": "1280x720",
    "3:2": "1280x720",
    "1:1": "1280x720",
    "9:16": "720x1280",
    "3:4": "720x1280",
    "2:3": "720x1280",
}
_JOB_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DONE = {"completed", "succeeded", "success"}
_FAIL = {"failed", "cancelled", "canceled", "error"}
_POLL_SECONDS = 5
_DROP_KEYS = ("duration", "resolution", "aspect_ratio", "generate_audio", "seconds", "size")


class VideoError(Exception):
    """The video service refused the request or returned nothing usable."""


def default_model(base_url):
    _, kind = endpoint_for(base_url)
    return DEFAULT_OPENAI if kind == "openai" else DEFAULT_OPENROUTER


def model_for(agent):
    """The video model. A blank video model uses the short-clip default."""
    chosen = (getattr(agent, "video_model", "") or "").strip()
    if chosen:
        return chosen
    return default_model(getattr(agent, "base_url", ""))


def endpoint_for(base_url):
    """Return (url, kind) where kind is 'openai' or 'openrouter'."""
    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    if base.endswith("/videos"):
        base = base[: -len("/videos")]
    if "api.openai.com" in base:
        return base + "/videos", "openai"
    return base + "/videos", "openrouter"


def clip_duration(value):
    """A short-clip length in seconds. Anything longer becomes 8."""
    if value in (None, ""):
        return DEFAULT_DURATION
    text = str(value).strip().lower()
    if text.endswith("s"):
        text = text[:-1].strip()
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return DEFAULT_DURATION
    if n >= 8:
        return 8
    if n <= 4:
        return 4
    return max(d for d in SHORT if d <= n)


def clip_aspect(value):
    ratio = (value or "16:9").strip()
    return ratio if ratio in ASPECTS else "16:9"


def frame_from_bytes(blob):
    """Return (media_type, bytes) for an image used as the opening frame."""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise VideoError("The starting image is empty.")
    if len(blob) > 8 * 1024 * 1024:
        raise VideoError("The starting image is too large.")
    media = _image_type(bytes(blob))
    if not media:
        raise VideoError("The starting image must be a PNG, JPEG, or WebP.")
    return media, bytes(blob)


def generate(agent, prompt, aspect_ratio="16:9", duration=DEFAULT_DURATION, first_frame=None):
    """Return (bytes, media_type, model, seconds). Raises VideoError."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise VideoError("Describe the clip to generate.")
    if len(prompt) > 4000:
        raise VideoError("The clip description is too long (4000 characters).")
    if getattr(agent, "provider", "") != "openai":
        raise VideoError("Video generation needs an OpenAI-compatible agent, such as OpenRouter or OpenAI.")
    if not getattr(agent, "api_key", ""):
        raise VideoError("This agent has no API key, so it cannot generate a video.")
    url, kind = endpoint_for(getattr(agent, "base_url", ""))
    seconds = clip_duration(duration)
    if kind == "openai" and seconds not in (4, 8):
        seconds = 4 if seconds < 6 else 8
    ratio = clip_aspect(aspect_ratio)
    model = model_for(agent)
    headers = _headers(agent)
    payload = _payload(kind, model, prompt, ratio, seconds, first_frame)
    timeout = getattr(settings, "PSEDITOR_VIDEO_TIMEOUT", 300)
    data = _submit(url, headers, payload)
    state = _state(data)
    if state in _FAIL:
        raise VideoError(_fail_text(data))
    poll_url = _poll_url(url, data, _job_id(data))
    if state not in _DONE:
        data = _wait(poll_url, headers, timeout)
    blob, media = _download(data, poll_url, url, headers)
    return blob, media, model, seconds


def list_models(base_url, api_key, extra_headers=None):
    """Return [{"id", "durations"}] for the video models this endpoint offers."""
    url, kind = endpoint_for(base_url)
    if kind == "openai":
        return [
            {"id": DEFAULT_OPENAI, "durations": [4, 8, 12]},
            {"id": "sora-2-pro", "durations": [4, 8, 12]},
        ]
    headers = dict(extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = _get(url + "/models", headers, 30)
    _reject_redirect(resp)
    if resp.status_code >= 400:
        raise VideoError(_error_message(resp) or f"Video service returned {resp.status_code}.")
    try:
        items = resp.json().get("data", [])
    except (ValueError, AttributeError):
        raise VideoError("The video service did not return a model list.")
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item["id"]),
                "durations": item.get("supported_durations") or [],
            })
    out.sort(key=lambda row: row["id"])
    if not out:
        raise VideoError("The video service returned no video models.")
    return out


def _payload(kind, model, prompt, ratio, seconds, first_frame):
    if kind == "openai":
        if first_frame:
            raise VideoError("This video service cannot start a clip from an image. Describe the clip instead.")
        return {"model": model, "prompt": prompt, "seconds": str(seconds), "size": _OPENAI_SIZE[ratio]}
    payload = {
        "model": model,
        "prompt": prompt,
        "duration": seconds,
        "resolution": "720p",
        "aspect_ratio": ratio,
        "generate_audio": False,
    }
    if first_frame:
        media, blob = _frame(first_frame)
        payload["frame_images"] = [{
            "type": "image_url",
            "image_url": {"url": "data:%s;base64,%s" % (media, base64.b64encode(blob).decode("ascii"))},
            "frame_type": "first_frame",
        }]
    return payload


def _frame(first_frame):
    if isinstance(first_frame, tuple):
        media, blob = frame_from_bytes(first_frame[1])
        return (first_frame[0] or media), blob
    return frame_from_bytes(first_frame)


def _headers(agent):
    headers = {"Authorization": f"Bearer {agent.api_key}"}
    extra = agent.headers() if hasattr(agent, "headers") else {}
    headers.update(extra)
    return headers


def _submit(url, headers, payload):
    post_headers = dict(headers)
    post_headers["Content-Type"] = "application/json"
    resp = _post(url, post_headers, payload)
    if resp.status_code == 400:
        retry = dict(payload)
        message = _error_message(resp).lower()
        for key in _DROP_KEYS:
            if key in message:
                retry.pop(key, None)
        if retry != payload:
            resp = _post(url, post_headers, retry)
    _reject_redirect(resp)
    if resp.status_code >= 400:
        raise VideoError(_error_message(resp) or f"Video service returned {resp.status_code}.")
    data = _json_object(resp)
    _job_id(data)
    return data


def _wait(poll_url, headers, timeout):
    deadline = time.monotonic() + max(1, int(timeout))
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        time.sleep(min(_POLL_SECONDS, max(0.0, remaining)))
        if time.monotonic() >= deadline:
            break
        resp = _get(poll_url, headers, 30)
        _reject_redirect(resp)
        if resp.status_code >= 400:
            raise VideoError(_error_message(resp) or f"Video service returned {resp.status_code}.")
        data = _json_object(resp)
        state = _state(data)
        if state in _DONE:
            return data
        if state in _FAIL:
            raise VideoError(_fail_text(data))
    raise VideoError("The clip took too long. Try again in a moment.")


def _download(status, poll_url, submit_url, headers):
    content_url = _content_url(status, poll_url, submit_url)
    resp = _get(content_url, headers, 180)
    _reject_redirect(resp)
    if resp.status_code >= 400:
        raise VideoError(_error_message(resp) or f"Video service returned {resp.status_code}.")
    blob = resp.content or b""
    media = _video_type(blob)
    if not media:
        raise VideoError("The video service returned no video data.")
    limit = getattr(settings, "PSEDITOR_MAX_VIDEO_BYTES", 40 * 1024 * 1024)
    if len(blob) > limit:
        raise VideoError("The generated clip is too large to save.")
    return blob, media


def _job_id(data):
    job_id = str((data or {}).get("id") or "")
    if not _JOB_ID.match(job_id):
        raise VideoError("The video service did not return a job id.")
    return job_id


def _poll_url(submit_url, data, job_id):
    raw = str((data or {}).get("polling_url") or "")
    if raw:
        absolute = urljoin(submit_url, raw)
        if _same_origin(absolute, submit_url):
            return absolute
    return submit_url.rstrip("/") + "/" + job_id


def _content_url(status, poll_url, submit_url):
    raw = _first_url(status)
    if raw:
        absolute = urljoin(poll_url, raw)
        if _same_origin(absolute, submit_url):
            return absolute
    return poll_url.rstrip("/") + "/content"


def _first_url(status):
    urls = (status or {}).get("unsigned_urls") or []
    if not urls:
        return ""
    item = urls[0]
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("url") or "")
    return ""


def _same_origin(url, origin):
    here, there = urlparse(url), urlparse(origin)
    if here.scheme not in ("https", "http") or here.scheme != there.scheme:
        return False
    if here.scheme == "http" and (here.hostname or "") not in ("127.0.0.1", "localhost"):
        return False
    return ((here.hostname or "").lower(), here.port) == ((there.hostname or "").lower(), there.port)


def _state(data):
    return str((data or {}).get("status") or "").strip().lower()


def _video_type(blob):
    if len(blob) >= 12 and b"ftyp" in blob[:32]:
        return "video/mp4"
    if blob.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return ""


def _image_type(blob):
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _json_object(resp):
    try:
        data = resp.json()
    except ValueError:
        raise VideoError("Video service returned a response that was not JSON.")
    if not isinstance(data, dict):
        raise VideoError("Video service returned an unexpected response.")
    return data


def _post(url, headers, payload):
    try:
        return requests.post(url, headers=headers, json=payload, timeout=120, allow_redirects=False)
    except requests.RequestException as exc:
        raise VideoError(f"Could not reach the video service: {exc}")


def _get(url, headers, timeout):
    try:
        return requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise VideoError(f"Could not reach the video service: {exc}")


def _fail_text(data):
    text = _error_from_body(data)
    return text or "Video generation failed."


def _reject_redirect(resp):
    if getattr(resp, "status_code", 0) in (301, 302, 303, 307, 308):
        raise VideoError("The video service redirected the request.")


def _error_message(resp):
    if getattr(resp, "status_code", 0) in (301, 302, 303, 307, 308):
        return "The video service redirected the request."
    try:
        data = resp.json()
    except (ValueError, AttributeError):
        return ""
    return _error_from_body(data)


def _error_from_body(data):
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")
    if isinstance(err, str):
        return err
    return str(data.get("message") or "")
