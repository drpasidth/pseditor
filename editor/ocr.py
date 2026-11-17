"""Send a workspace image to the OCR endpoint from settings."""
import base64
import io
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from django.conf import settings

from .fsops import FsError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TIMEOUT = 90
_TYPHOON_PROMPT = (
    "Extract all text from the image.\n"
    "Only return the clean Markdown. Do not add an explanation.\n"
    "Include every piece of writing on the page, including Thai text.\n"
    "Render tables as HTML. Describe figures in Thai."
)


def _config_path():
    raw = (getattr(settings, "PSEDITOR_OCR_CONFIG", "") or "").strip()
    if raw:
        return Path(raw)
    return Path(settings.BASE_DIR) / "ocr_settings.json"


def _saved():
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick(saved_key, setting_name):
    saved = _saved()
    if saved_key in saved:
        return str(saved.get(saved_key) or "").strip()
    return (getattr(settings, setting_name, "") or "").strip()


def endpoint():
    return _pick("endpoint", "PSEDITOR_OCR_ENDPOINT")


def api_key():
    return _pick("api_key", "PSEDITOR_OCR_API_KEY")


def model():
    return _pick("model", "PSEDITOR_OCR_MODEL")


def public():
    """Values safe to show in the editor. The API key itself stays on the server."""
    return {"endpoint": endpoint(), "model": model(), "has_key": bool(api_key())}


def save(endpoint_value, model_value, api_key_value):
    """Store the OCR endpoint. A blank API key keeps the one already saved."""
    endpoint_value = str(endpoint_value or "").strip()
    model_value = str(model_value or "").strip()
    api_key_value = str(api_key_value or "").strip()
    if endpoint_value and not endpoint_value.lower().startswith(("http://", "https://")):
        raise FsError("OCR endpoint must be an http or https URL.")
    if not api_key_value:
        api_key_value = api_key()
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "endpoint": endpoint_value,
        "model": model_value,
        "api_key": api_key_value,
    }), encoding="utf-8")
    return public()


def recognize_bytes(raw, filename, mime="application/octet-stream"):
    """Read text from image bytes using the saved OCR endpoint."""
    url = endpoint()
    if not url:
        raise FsError("OCR is not configured. Set the endpoint with the gear icon.")
    return _recognize(url, api_key(), model(), raw, filename, mime)


_CHAT_CACHE = {}


def text_for_chat_images(images):
    """OCR images attached to a chat message. The same picture is not sent twice."""
    if not endpoint() or not images:
        return ""
    chunks = []
    for img in images:
        data = img.get("data") or ""
        name = img.get("name") or "image"
        key = (name, len(data), data[:48], data[-48:])
        if key not in _CHAT_CACHE:
            try:
                raw = base64.b64decode(data)
                found = recognize_bytes(raw, name, img.get("mime") or "image/png").strip()
                _CHAT_CACHE[key] = found or "(no text found)"
            except (FsError, ValueError) as exc:
                _CHAT_CACHE[key] = f"(OCR failed: {exc})"
        chunks.append(f"### {name}\n{_CHAT_CACHE[key]}")
    return "\n\n".join(chunks)


def read_text(fs, rel):
    """Return the text the configured OCR service read from an image file."""
    url = endpoint()
    if not url:
        raise FsError(
            "OCR is not configured. Set the endpoint with the gear icon, or in pseditor/settings.py."
        )
    path = fs.resolve(rel)
    if not path.is_file():
        raise FsError(f"File not found: {rel}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise FsError("OCR reads image files (png, jpg, gif, webp, bmp).")
    ctype, _ = mimetypes.guess_type(path.name)
    return _recognize(url, api_key(), model(), path.read_bytes(), path.name, ctype or "application/octet-stream")


def test_connection(endpoint_value, model_value, api_key_value):
    """POST a tiny image to the endpoint the user is editing. Returns (ok, message)."""
    url = str(endpoint_value if endpoint_value is not None else endpoint()).strip()
    model_name = str(model_value if model_value is not None else model()).strip()
    key = str(api_key_value or "").strip() or api_key()
    if not url:
        return False, "Set an OCR endpoint first."
    try:
        text = _recognize(url, key, model_name, _sample_png(), "sample.png", "image/png")
    except FsError as exc:
        return False, str(exc)
    snippet = " ".join((text or "").split())[:160]
    if not snippet:
        return True, "Connected. The endpoint accepted the test image."
    return True, "Connected. Sample: " + snippet


def _recognize(url, key, model_name, raw, filename, mime):
    if not str(url or "").lower().startswith(("http://", "https://")):
        raise FsError("OCR endpoint must be an http or https URL.")
    try:
        if _is_typhoon(url):
            resp = _post_typhoon(url, key, model_name, raw)
        else:
            resp = _post_file(url, key, model_name, raw, filename, mime)
    except requests.RequestException as exc:
        raise FsError(f"OCR endpoint could not be reached: {exc}")
    if resp.status_code >= 400:
        raise FsError(f"OCR endpoint returned {resp.status_code}. {_error_detail(resp)}".strip())
    return _text_from(resp) or ""


def _is_typhoon(url):
    return urlparse(url).netloc.lower().endswith("opentyphoon.ai")


def _typhoon_url(url):
    """Typhoon reads images through chat completions, not the /ocr path."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return url
    if path.endswith("/ocr"):
        path = path[: -len("/ocr")] + "/chat/completions"
    else:
        path = path + "/chat/completions"
    return urlunparse(parsed._replace(path=path))


def _post_typhoon(url, key, model_name, raw):
    data_url = _jpeg_data_url(raw)
    payload = {
        "model": model_name or "typhoon-ocr",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _TYPHOON_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": 4096,
        "temperature": 0.1,
        "top_p": 0.6,
        "repetition_penalty": 1.1,
    }
    headers = {"Content-Type": "application/json", **_auth_header(key)}
    return requests.post(_typhoon_url(url), headers=headers, json=payload, timeout=_TIMEOUT)


def _post_file(url, key, model_name, raw, filename, mime):
    data = {"model": model_name} if model_name else None
    files = {"file": (filename, io.BytesIO(raw), mime or "application/octet-stream")}
    return requests.post(url, headers=_auth_header(key), data=data, files=files, timeout=_TIMEOUT)


def _jpeg_data_url(raw):
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    longest = max(im.size)
    if longest > 1800:
        scale = 1800 / float(longest)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + encoded


def _sample_png():
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (160, 48), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle((4, 4, 155, 43), outline=(0, 0, 0))
    draw.text((12, 16), "OCR test", fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _error_detail(resp):
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
        if isinstance(data.get("message"), str):
            return data["message"][:300]
    return (resp.text or "").strip().replace("\n", " ")[:300]


def _auth_header(key):
    if not key:
        return {}
    if key.lower().startswith("bearer "):
        return {"Authorization": key}
    return {"Authorization": f"Bearer {key}"}


def _text_from(resp):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    body = resp.text or ""
    if "json" in ctype or body.lstrip()[:1] in "{[":
        try:
            data = resp.json()
        except ValueError:
            data = None
        found = _find_text(data)
        if found is not None:
            return found
    return body


def _find_text(data):
    """Pull readable text out of a plain string or a Typhoon OCR JSON body."""
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, list):
        parts = [text for item in data if (text := _find_text(item))]
        return "\n\n".join(parts) if parts else None
    if not isinstance(data, dict):
        return None
    for key in ("text", "natural_text", "markdown"):
        if isinstance(data.get(key), str) and data[key].strip():
            return data[key].strip()
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        found = _find_text(choices[0].get("message") if isinstance(choices[0], dict) else None)
        if found:
            return found
    for key in ("results", "pages", "data", "message"):
        if key in data:
            found = _find_text(data[key])
            if found:
                return found
    if isinstance(data.get("content"), str) and data["content"].strip():
        return data["content"].strip()
    return None
