"""Connectors for external agents and the tool-calling loop that drives them."""
import json
import os
import re
import shlex
import shutil
import sys
import uuid

import requests
from django.conf import settings

from . import approvals, ocr
from .fsops import FsError, run_process
from .models import Agent, ChatMessage
from .tools import TOOL_SPECS, ToolContext, tool_specs

HTTP_TIMEOUT = 300
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

DEFAULT_SYSTEM_PROMPT = """You are the coding agent inside PSEditor, a code editor similar to Cursor.
You work on the user's workspace through tools: workspace_info, list_files, read_file, search, write_file, edit_file, delete_file, generate_image and run_command.
The workspace is the set of folders the user has added to it; you can read, create, update and delete files inside those folders only.
The user can add more folders at any time, so call workspace_info if you are unsure which folders are available.

Guidelines:
- Read relevant files before changing them. Paths are relative to the workspace root.
- Prefer edit_file for small, targeted changes; use write_file for new files or full rewrites.
- Make the changes the user asked for directly with tools instead of only describing them.
- After changing code, verify it when practical (run tests or the program with run_command).
- Every write_file, edit_file, delete_file and run_command is shown to the user, who must approve it before it runs. If an action is denied, do not retry it unchanged: take the user's reason into account, try a different approach, or ask what they want.
- Keep final answers short: say what you changed and anything the user must do next.
- When the user asks you to create, draw, or generate a picture, call generate_image with a detailed prompt. It saves an image file in the workspace. Do not say an image was created unless that tool succeeded.
- The user may attach images, PDFs, Excel workbooks and Word documents. Text extracted from documents is in the user message inside <attachments>. Text read from images is inside <image-text>. read_file on a png, jpg, gif, webp or bmp also returns that text.
Operating system of the host: {os}.{multi}"""

ANALYTICS_SYSTEM_PROMPT = """You are the analytics agent inside PSEditor. Analytics mode is on.
You answer questions about the user's data: Excel workbooks, CSV files, SQLite databases, invoices, receipts, and financial statements.
Tools: list_datasets, profile_data, query_data, sql_query, extract_document, plus the usual file tools.

Rules:
- Do not invent a number, date, name, or total. Every figure you state must appear in a tool result or in an attached profile from this turn.
- For a CSV, Excel, or SQLite file, call profile_data before you answer, then query_data or sql_query for the total you need. sql_query is one read-only SELECT and only works on SQLite files.
- For a PDF, Word file, or image of an invoice, receipt, or financial statement, call extract_document. Quote the field value and its source line. If a field is absent, say it was not found.
- Images are read with the OCR endpoint. If that is not configured or it fails, say so instead of guessing the text.
- You may use write_file when the user asks to save a report. Do not change source data unless they ask.
- When the user asks for a picture, call generate_image. It saves an image file. Do not invent an image.
- Every write_file, edit_file, delete_file and run_command waits for the user's approval. Reads and analysis tools do not.
- Keep the answer short: the result, the figures, and the file they came from.
Operating system of the host: {os}.{multi}"""

MULTI_ROOT_NOTE = """
This workspace has multiple root folders. File paths use the form RootName/relative/path
(for example myapp/src/main.py). list_files shows all roots; run_command cwd may be a root name."""


class AgentError(Exception):
    pass


class Cancelled(Exception):
    """The user pressed Stop while an action was waiting for approval."""


def _ask(session, call_id, name, args):
    """Generator: ask the user before an action runs. Returns (allowed, note) through ``yield from``.

    Yields an ``approval`` event, then blocks until the browser answers. Raises Cancelled on Stop and
    AgentError on timeout, so an unanswered action is never executed.
    """
    if not approvals.required(name):
        return True, ""
    a = approvals.create(session.id, name, args)
    try:
        yield {"type": "approval", "id": a.id, "tool_id": call_id, "name": name, "arguments": args}
        decision = approvals.wait(a)
    finally:
        approvals.discard(a.id)  # also runs when the client disconnects mid-wait
    yield {"type": "approval_result", "id": a.id, "tool_id": call_id, "decision": decision}
    if decision == approvals.ALLOW:
        return True, ""
    if decision == approvals.DENY:
        return False, a.reason
    if decision == approvals.CANCELLED:
        raise Cancelled()
    raise AgentError("Timed out waiting for your approval, so nothing was run. Send a message to try again.")


def _denied_text(note):
    text = "The user denied this action, so it was NOT run."
    if note:
        text += f" Reason given by the user: {note}"
    return text + " Do not repeat it unchanged; adapt, try something else, or ask the user what they want."


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def build_system_prompt(agent, fs=None, analytics=False):
    multi = MULTI_ROOT_NOTE if fs is not None and getattr(fs, "multi", False) else ""
    template = ANALYTICS_SYSTEM_PROMPT if analytics else DEFAULT_SYSTEM_PROMPT
    base = template.format(
        os="Windows" if sys.platform == "win32" else sys.platform,
        multi=multi,
    )
    if fs is not None:
        base += "\n\nWorkspace folders:\n" + describe_folders(fs)
    if agent.system_prompt.strip():
        base += "\n\nAdditional instructions from the user:\n" + agent.system_prompt.strip()
    return base


def describe_folders(fs):
    """One line per workspace folder: how to address it and where it lives on disk."""
    if getattr(fs, "multi", False):
        return "\n".join(f"- {n}/  ->  {p}" for n, p in fs.folders)
    return "\n".join(f"- (workspace root)  ->  {p}" for _, p in fs.folders)


def build_context(fs, active=None):
    """Context appended to the user's message: open file, selection, file list."""
    parts = ["Workspace folders (the model can read/write inside these):\n" + describe_folders(fs)]
    files = fs.list_files(limit=200)
    if files:
        parts.append("Workspace files (first %d):\n%s" % (len(files), "\n".join(files)))
    if active and active.get("path"):
        block = f"Currently open file: {active['path']}"
        sel = (active.get("selection") or "").strip()
        if sel:
            block += f"\nSelected text:\n```\n{sel[:6000]}\n```"
        parts.append(block)
    return "\n\n<context>\n" + "\n\n".join(parts) + "\n</context>" if parts else ""


def _user_text(m):
    """Typed text plus extracted attachments, without the workspace <context> dump."""
    if m.get("role") != "user":
        return m.get("content") or ""
    return m.get("plain") or m.get("display") or m.get("content") or ""


def load_history(session, limit=60):
    rows = list(session.messages.order_by("-id")[:limit])[::-1]
    msgs = [r.data() for r in rows if r.role in ("user", "assistant", "tool")]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _save(session, msg):
    return ChatMessage.objects.create(session=session, role=msg["role"], payload=json.dumps(msg))


def _http_error(resp):
    try:
        body = resp.json()
        detail = body.get("error", body)
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
    except ValueError:
        detail = resp.text[:500]
    return AgentError(f"HTTP {resp.status_code}: {detail}")


def _post(url, headers, payload):
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise AgentError(f"Cannot reach agent at {url}: {exc}")
    if resp.status_code >= 400:
        raise _http_error(resp)
    return resp


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def resolve_openai_base(base_url, api_key):
    """Default the base URL; OpenRouter keys (sk-or-...) never belong on api.openai.com."""
    base = (base_url or "").strip().rstrip("/")
    if (api_key or "").startswith("sk-or-") and (not base or "api.openai.com" in base):
        return OPENROUTER_URL
    return base or "https://api.openai.com/v1"


def _openai_url(agent):
    base = resolve_openai_base(agent.base_url, agent.api_key).rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _openai_user_content(m):
    """String for a text turn; a content-part list when the turn has images."""
    images = m.get("images") or []
    text = m.get("content") or ""
    if not images:
        return text
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images:
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{img['mime']};base64,{img['data']}"},
        })
    return parts


THINKING_LEVELS = ("off", "low", "medium", "high")
# Anthropic counts thinking tokens inside max_tokens, and the budget must be smaller than that.
ANTHROPIC_THINKING = {"low": (2048, 8192), "medium": (8000, 16000), "high": (16000, 24000)}


def normalize_thinking(level):
    """One of off, low, medium, high. Anything else is off."""
    level = str(level or "off").strip().lower()
    return level if level in THINKING_LEVELS else "off"


def _reasoning_text(msg):
    """Plain thinking text from an OpenAI-compatible message, for the chat panel."""
    for key in ("reasoning", "reasoning_content"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = []
    for item in msg.get("reasoning_details") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("summary") or item.get("content") or ""
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


# Models that answered 404 because they cannot take an image. Later turns skip the picture.
_MODELS_WITHOUT_VISION = set()


def _image_rejected(exc):
    text = str(exc).lower()
    return "image input" in text or "support image" in text


def _history_has_images(messages):
    return any(m.get("images") for m in messages)


def _with_image_text(messages):
    """Copy user turns so a text-only model still receives what the OCR endpoint read."""
    out = []
    for m in messages:
        if m.get("role") == "user" and m.get("images") and "<image-text>" not in (m.get("content") or ""):
            block = ocr.text_for_chat_images(m["images"])
            if block:
                m = dict(m)
                m["content"] = ((m.get("content") or "").rstrip() + "\n\n<image-text>\n" + block + "\n</image-text>")
        out.append(m)
    return out


def _to_openai(system, messages, allow_images=True):
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            content = _openai_user_content(m) if allow_images else (m.get("content") or "")
            out.append({"role": "user", "content": content})
        elif m["role"] == "assistant":
            d = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                d["tool_calls"] = [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                } for tc in m["tool_calls"]]
            # OpenRouter rejects the next step of a tool call unless this comes back unchanged.
            if m.get("reasoning_details"):
                d["reasoning_details"] = m["reasoning_details"]
            out.append(d)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def call_openai(agent, system, messages, use_tools=True, thinking="off", tools=None):
    messages = _with_image_text(messages)
    model_key = (agent.model or "").strip()
    allow_images = model_key not in _MODELS_WITHOUT_VISION
    try:
        return _complete_openai(agent, system, messages, use_tools, thinking, allow_images, tools)
    except AgentError as exc:
        if not allow_images or not _image_rejected(exc) or not _history_has_images(messages):
            raise
        _MODELS_WITHOUT_VISION.add(model_key)
        if "<image-text>" not in json.dumps(messages) and not ocr.endpoint():
            raise AgentError(
                "This model cannot read images. Set the OCR endpoint with the gear icon, then send the image again. "
                + str(exc)
            )
        return _complete_openai(agent, system, messages, use_tools, thinking, False, tools)


def _complete_openai(agent, system, messages, use_tools, thinking, allow_images, tools=None):
    headers = {"Content-Type": "application/json", **agent.headers()}
    if agent.api_key:
        headers["Authorization"] = f"Bearer {agent.api_key}"
    payload = {"model": agent.model, "messages": _to_openai(system, messages, allow_images)}
    if agent.temperature is not None:
        payload["temperature"] = agent.temperature
    level = normalize_thinking(thinking)
    base = resolve_openai_base(agent.base_url, agent.api_key)
    if level != "off" and "openrouter.ai" in base:
        payload["reasoning"] = {"effort": level}
    elif level != "off" and "api.openai.com" in base:
        payload["reasoning_effort"] = level
    specs = TOOL_SPECS if tools is None else tools
    if use_tools and specs:
        payload["tools"] = [{"type": "function", "function": t} for t in specs]
        payload["tool_choice"] = "auto"
    data = _post(_openai_url(agent), headers, payload).json()
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise AgentError(f"Unexpected response: {str(data)[:300]}")
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        calls.append({
            "id": tc.get("id") or "call_" + uuid.uuid4().hex[:12],
            "name": fn.get("name", ""),
            "arguments": _parse_args(fn.get("arguments")),
        })
    details = msg.get("reasoning_details")
    return {
        "content": msg.get("content") or "",
        "tool_calls": calls,
        "thinking": _reasoning_text(msg),
        "reasoning_details": details if isinstance(details, list) else None,
    }


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
def _anthropic_url(agent):
    base = (agent.base_url or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _anthropic_user_blocks(m):
    blocks = []
    for img in m.get("images") or []:
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["mime"], "data": img["data"]},
        })
    blocks.append({"type": "text", "text": m.get("content") or "(empty)"})
    return blocks


def _to_anthropic(messages, allow_images=True):
    out = []

    def push(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m["role"] == "user":
            push("user", _anthropic_user_blocks(m) if allow_images else [
                {"type": "text", "text": m.get("content") or "(empty)"},
            ])
        elif m["role"] == "assistant":
            blocks = []
            for block in m.get("thinking_blocks") or []:
                if isinstance(block, dict):
                    blocks.append(block)
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                               "input": tc["arguments"]})
            if blocks:
                push("assistant", blocks)
        elif m["role"] == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                           "content": m["content"] or "(empty)"}])
    return out


def call_anthropic(agent, system, messages, use_tools=True, thinking="off", tools=None):
    messages = _with_image_text(messages)
    model_key = (agent.model or "").strip()
    allow_images = model_key not in _MODELS_WITHOUT_VISION
    try:
        return _complete_anthropic(agent, system, messages, use_tools, thinking, allow_images, tools)
    except AgentError as exc:
        if not allow_images or not _image_rejected(exc) or not _history_has_images(messages):
            raise
        _MODELS_WITHOUT_VISION.add(model_key)
        if "<image-text>" not in json.dumps(messages) and not ocr.endpoint():
            raise AgentError(
                "This model cannot read images. Set the OCR endpoint with the gear icon, then send the image again. "
                + str(exc)
            )
        return _complete_anthropic(agent, system, messages, use_tools, thinking, False, tools)


def _complete_anthropic(agent, system, messages, use_tools, thinking, allow_images, tools=None):
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": agent.api_key,
        **agent.headers(),
    }
    payload = {
        "model": agent.model or "claude-sonnet-4-5",
        "max_tokens": 8192,
        "system": system,
        "messages": _to_anthropic(messages, allow_images),
    }
    level = normalize_thinking(thinking)
    if level != "off":
        budget, limit = ANTHROPIC_THINKING[level]
        payload["max_tokens"] = limit
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Claude rejects any temperature other than 1 while extended thinking is on.
        payload.pop("temperature", None)
    elif agent.temperature is not None:
        payload["temperature"] = agent.temperature
    specs = TOOL_SPECS if tools is None else tools
    if use_tools and specs:
        payload["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in specs
        ]
    data = _post(_anthropic_url(agent), headers, payload).json()
    text, calls, think_text, thinking_blocks = [], [], [], []
    for block in data.get("content", []):
        kind = block.get("type")
        if kind == "text":
            text.append(block.get("text", ""))
        elif kind == "thinking":
            thinking_blocks.append(block)
            if block.get("thinking"):
                think_text.append(block["thinking"])
        elif kind == "redacted_thinking":
            thinking_blocks.append(block)
        elif kind == "tool_use":
            calls.append({"id": block["id"], "name": block["name"], "arguments": block.get("input") or {}})
    return {
        "content": "".join(text),
        "tool_calls": calls,
        "thinking": "\n\n".join(think_text),
        "thinking_blocks": thinking_blocks,
    }


# ---------------------------------------------------------------------------
# Webhook & CLI agents
# ---------------------------------------------------------------------------
def call_webhook(agent, fs, instruction, history, active, analytics=False):
    """POST the task to a custom HTTP agent.

    Request JSON: instruction, messages[{role,content}], system_prompt, model, workspace{name,path},
                  files[], active_file{path,selection,content}
    Response JSON: {"reply": str, "actions": [{"type": "write_file"|"edit_file"|"delete_file"|
                    "run_command", ...tool arguments...}]}
    """
    if not agent.base_url:
        raise AgentError("Webhook URL is not configured.")
    headers = {"Content-Type": "application/json", **agent.headers()}
    if agent.api_key:
        headers["Authorization"] = f"Bearer {agent.api_key}"
    active_payload = None
    if active and active.get("path"):
        active_payload = dict(active)
        try:
            active_payload["content"] = fs.read(active["path"])[:60000]
        except FsError:
            pass
    payload = {
        "instruction": instruction,
        "messages": [
            {"role": m["role"], "content": _user_text(m) if m["role"] == "user" else m["content"]}
            for m in history if m["role"] in ("user", "assistant") and (m.get("content") or m.get("plain"))
        ],
        "system_prompt": build_system_prompt(agent, fs, analytics=analytics),
        "analytics": bool(analytics),
        "model": agent.model,
        "workspace": {
            "name": fs.root.name,
            "path": str(fs.root),
            "folders": [{"name": n, "path": str(p)} for n, p in fs.folders],
        },
        "files": fs.list_files(limit=500),
        "active_file": active_payload,
    }
    last_user = next((m for m in reversed(history) if m.get("role") == "user"), None)
    if last_user and last_user.get("attachments"):
        payload["attachments"] = last_user["attachments"]
    if last_user and last_user.get("images"):
        payload["images"] = last_user["images"]
    resp = _post(agent.base_url, headers, payload)
    try:
        data = resp.json()
    except ValueError:
        return {"reply": resp.text, "actions": []}
    if not isinstance(data, dict):
        return {"reply": str(data), "actions": []}
    return {
        "reply": data.get("reply") or data.get("message") or data.get("content") or "",
        "actions": data.get("actions") or [],
    }


def _cli_prompt(agent, history, instruction, context, analytics=False):
    parts = []
    if analytics:
        parts.append(
            "Analytics mode is on. Do not invent figures. "
            "Profile CSV, Excel, and SQLite before quoting a total, and read invoices, receipts, "
            "and financial statements from the file text before quoting a field."
        )
    if agent.system_prompt.strip():
        parts.append(agent.system_prompt.strip())
    turns = [m for m in history[:-1] if m["role"] in ("user", "assistant") and m.get("content")][-8:]
    if turns:
        convo = []
        for m in turns:
            who = "User" if m["role"] == "user" else "Assistant"
            convo.append(f"{who}: {_user_text(m) if m['role'] == 'user' else m['content']}")
        parts.append("Earlier conversation:\n" + "\n".join(convo))
    parts.append(instruction + context)
    return "\n\n".join(parts)


def call_cli(agent, fs, prompt):
    cmd = agent.command.strip()
    if not cmd:
        raise AgentError("No command configured for this agent.")
    timeout = getattr(settings, "PSEDITOR_CLI_TIMEOUT", 900)
    if "{prompt}" in cmd:
        posix = os.name != "nt"
        args = shlex.split(cmd, posix=posix)
        if not posix:
            args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
        exe = shutil.which(args[0])
        if exe:
            args[0] = exe
        flat = exe and exe.lower().endswith((".cmd", ".bat"))
        value = prompt.replace("\r", "").replace("\n", " ") if flat else prompt
        args = [a.replace("{prompt}", value) for a in args]
        code, out = run_process(args, fs.root, timeout, shell=False)
    else:
        code, out = run_process(cmd, fs.root, timeout, shell=True, stdin=prompt)
    out = ANSI_RE.sub("", out).strip()
    if code != 0:
        out = (out + f"\n\n[exit code {code}]").strip()
    return out or "(agent produced no output)"


# ---------------------------------------------------------------------------
# model listing
# ---------------------------------------------------------------------------
def list_models(provider, base_url, api_key, extra_headers=None):
    """Return [{"id": str, "tools": True|False|None}] from the provider's /models endpoint."""
    headers = dict(extra_headers or {})
    if provider == Agent.OPENAI:
        base = resolve_openai_base(base_url, api_key).rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        url = base + "/models"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    elif provider == Agent.ANTHROPIC:
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        raise AgentError("Model listing is only available for OpenAI-compatible and Anthropic agents.")
    try:
        resp = requests.get(url, headers=headers, params={"limit": 1000} if provider == Agent.ANTHROPIC else None,
                            timeout=30)
    except requests.RequestException as exc:
        raise AgentError(f"Cannot reach {url}: {exc}")
    if resp.status_code >= 400:
        raise _http_error(resp)
    try:
        items = resp.json().get("data", [])
    except (ValueError, AttributeError):
        raise AgentError("The provider did not return a JSON model list.")
    out = []
    for m in items:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        params = m.get("supported_parameters")  # OpenRouter reports tool support here
        tools = ("tools" in params) if isinstance(params, list) else None
        out.append({"id": m["id"], "tools": tools})
    out.sort(key=lambda x: x["id"])
    return out


# ---------------------------------------------------------------------------
# connection test
# ---------------------------------------------------------------------------
def test_agent(agent):
    """Return (ok, message)."""
    try:
        if agent.provider == Agent.OPENAI:
            r = call_openai(agent, "You are a test.", [{"role": "user", "content": "Reply with: OK"}], use_tools=False)
            return True, f"Connected. Model replied: {r['content'][:80]!r}"
        if agent.provider == Agent.ANTHROPIC:
            r = call_anthropic(agent, "You are a test.", [{"role": "user", "content": "Reply with: OK"}], use_tools=False)
            return True, f"Connected. Model replied: {r['content'][:80]!r}"
        if agent.provider == Agent.WEBHOOK:
            if not agent.base_url:
                return False, "Webhook URL is empty."
            headers = {"Content-Type": "application/json", **agent.headers()}
            if agent.api_key:
                headers["Authorization"] = f"Bearer {agent.api_key}"
            _post(agent.base_url, headers, {"instruction": "ping", "ping": True, "messages": [],
                                           "files": [], "workspace": {}})
            return True, "Webhook responded."
        if agent.provider == Agent.CLI:
            parts = shlex.split(agent.command, posix=os.name != "nt") if agent.command.strip() else []
            if not parts:
                return False, "Command is empty."
            exe = shutil.which(parts[0].strip("\"'"))
            return (True, f"Found executable: {exe}") if exe else (False, f"'{parts[0]}' was not found on PATH.")
    except AgentError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Error: {exc}"
    return False, "Unknown provider."


# ---------------------------------------------------------------------------
# the agent turn
# ---------------------------------------------------------------------------
def run_turn(session, agent, fs, text, active=None, prepared=None, thinking="off", analytics=False):
    """Generator of event dicts. Persists messages and an undo checkpoint.

    ``prepared`` is an attachments.Prepared value. Images go to vision models as image
    blocks; text extracted from PDF, Excel and Word is part of the user message.
    """
    ctx = ToolContext(fs, agent)
    context = build_context(fs, active)
    history = load_history(session)
    plain = text
    if prepared is not None and prepared.text:
        plain = (text + "\n\n" + prepared.text).strip() if text else prepared.text
    if not plain and prepared is not None and prepared.images:
        plain = "Please look at the attached image(s)."
    user_msg = {"role": "user", "content": plain + context, "display": text}
    if prepared is not None and prepared.meta:
        user_msg["plain"] = plain
        user_msg["attachments"] = prepared.meta
    if prepared is not None and prepared.images:
        user_msg["images"] = prepared.images
        block = ocr.text_for_chat_images(prepared.images)
        if block:
            if analytics:
                from .analytics import field_note
                note = field_note(block)
                if note:
                    block = block + "\n\n" + note
            plain = (plain + "\n\n<image-text>\n" + block + "\n</image-text>").strip()
            user_msg["content"] = plain + context
            user_msg["plain"] = plain
    if analytics and active and active.get("path"):
        from .analytics import open_file_note
        note = open_file_note(fs, active["path"])
        if note:
            plain = (plain + "\n\n" + note).strip()
            user_msg["content"] = plain + context
            user_msg["plain"] = plain
    user_row = _save(session, user_msg)
    history.append(user_msg)
    if session.title == "New chat":
        title_src = text or (prepared.meta[0]["name"] if prepared is not None and prepared.meta else "")
        session.title = title_src.strip().splitlines()[0][:60] or "New chat"
    session.agent = agent
    session.save()

    yield {"type": "start", "message_id": user_row.id}
    try:
        if agent.provider in (Agent.OPENAI, Agent.ANTHROPIC):
            yield from _tool_loop(session, agent, ctx, history, fs=fs, thinking=thinking, analytics=analytics)
        elif agent.provider == Agent.WEBHOOK:
            yield from _webhook_turn(session, agent, fs, ctx, history, plain, active, analytics=analytics)
        elif agent.provider == Agent.CLI:
            yield from _cli_turn(session, agent, fs, ctx, history, plain, context, analytics=analytics)
        else:
            raise AgentError(f"Unknown provider {agent.provider}")
    except Cancelled:
        yield {"type": "error", "message": "Stopped while waiting for approval. Nothing more was run."}
    except AgentError as exc:
        yield {"type": "error", "message": str(exc)}
    except GeneratorExit:
        raise
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"Unexpected error: {exc}"}
    finally:
        if ctx.checkpoint:
            user_row.checkpoint = ctx.checkpoint_json()
            user_row.save(update_fields=["checkpoint"])
    if ctx.changed:
        yield {"type": "files_changed", "paths": ctx.changed}
    yield {"type": "done", "message_id": user_row.id, "changed": ctx.changed, "title": session.title}


def _remember_thinking(assistant, reply):
    """Keep reasoning on the saved message so the next step and the chat can both use it."""
    if reply.get("thinking"):
        assistant["thinking"] = reply["thinking"]
    if reply.get("reasoning_details"):
        assistant["reasoning_details"] = reply["reasoning_details"]
    if reply.get("thinking_blocks"):
        assistant["thinking_blocks"] = reply["thinking_blocks"]


def _tool_loop(session, agent, ctx, history, fs=None, thinking="off", analytics=False):
    system = build_system_prompt(agent, fs, analytics=analytics)
    caller = call_openai if agent.provider == Agent.OPENAI else call_anthropic
    level = normalize_thinking(thinking)
    specs = tool_specs(analytics)
    max_steps = getattr(settings, "PSEDITOR_MAX_AGENT_STEPS", 25)
    for _ in range(max_steps):
        reply = caller(agent, system, history, thinking=level, tools=specs)
        assistant = {"role": "assistant", "content": reply["content"], "tool_calls": reply["tool_calls"]}
        _remember_thinking(assistant, reply)
        if reply.get("thinking"):
            yield {"type": "thinking", "content": reply["thinking"]}
        if reply["content"]:
            yield {"type": "text", "content": reply["content"]}
        if not reply["tool_calls"]:
            history.append(assistant)
            _save(session, assistant)
            return
        results = []
        for tc in reply["tool_calls"]:
            yield {"type": "tool_call", "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]}
            allowed, note = yield from _ask(session, tc["id"], tc["name"], tc["arguments"])
            if allowed:
                ok, result = ctx.execute(tc["name"], tc["arguments"])
            else:
                ok, result = False, _denied_text(note)
            yield {"type": "tool_result", "id": tc["id"], "name": tc["name"], "ok": ok,
                   "result": result[:4000], "changed": list(ctx.changed)}
            results.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": result})
        history.append(assistant)
        history.extend(results)
        _save(session, assistant)
        for r in results:
            _save(session, r)
    note = {"role": "assistant", "content": f"(Stopped after {max_steps} steps. Send a message to continue.)"}
    history.append(note)
    _save(session, note)
    yield {"type": "text", "content": note["content"]}


def _webhook_turn(session, agent, fs, ctx, history, text, active, analytics=False):
    data = call_webhook(agent, fs, text, history, active, analytics=analytics)
    for i, action in enumerate(data["actions"]):
        if not isinstance(action, dict):
            continue
        name = action.get("type") or action.get("action") or ""
        args = {k: v for k, v in action.items() if k not in ("type", "action")}
        cid = f"act_{i}"
        yield {"type": "tool_call", "id": cid, "name": name, "arguments": args}
        allowed, note = yield from _ask(session, cid, name, args)
        if allowed:
            ok, result = ctx.execute(name, args)
        else:
            ok, result = False, _denied_text(note)
        yield {"type": "tool_result", "id": cid, "name": name, "ok": ok, "result": result[:4000],
               "changed": list(ctx.changed)}
    reply = {"role": "assistant", "content": data["reply"]}
    history.append(reply)
    _save(session, reply)
    if data["reply"]:
        yield {"type": "text", "content": data["reply"]}


def _cli_turn(session, agent, fs, ctx, history, text, context, analytics=False):
    prompt = _cli_prompt(agent, history, text, context, analytics=analytics)
    before = fs.snapshot()
    args = {"command": agent.command, "prompt": prompt[:3000]}
    yield {"type": "tool_call", "id": "cli", "name": "cli_agent", "arguments": args}
    # A CLI agent edits files by itself and cannot be interrupted per action, so approve the whole run up front.
    allowed, note = yield from _ask(session, "cli", "cli_agent", args)
    if not allowed:
        msg = "Not run: you denied this command-line agent." + (f" Reason: {note}" if note else "")
        yield {"type": "tool_result", "id": "cli", "name": "cli_agent", "ok": False, "result": msg,
               "changed": []}
        reply = {"role": "assistant", "content": msg}
        history.append(reply)
        _save(session, reply)
        yield {"type": "text", "content": msg}
        return
    out = call_cli(agent, fs, prompt)
    ctx.absorb_snapshot_diff(before, fs.snapshot())
    yield {"type": "tool_result", "id": "cli", "name": "cli_agent", "ok": True,
           "result": f"finished; {len(ctx.changed)} file(s) changed", "changed": list(ctx.changed)}
    reply = {"role": "assistant", "content": out}
    history.append(reply)
    _save(session, reply)
    yield {"type": "text", "content": out}
