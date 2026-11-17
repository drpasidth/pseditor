import json
import mimetypes
import os
import re
import sys
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from . import agents as agent_mod
from . import approvals, ocr, ptyterm, shell
from .attachments import prepare_attachments
from .fsops import FsError, WorkspaceFS, folder_label
from .models import Agent, ChatMessage, ChatSession, Workspace
from .tools import revert


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def api(methods):
    """JSON in/out wrapper with uniform error handling."""

    def deco(fn):
        @wraps(fn)
        @require_http_methods(methods)
        def wrapper(request, *args, **kwargs):
            request.json = {}
            if request.body:
                try:
                    request.json = json.loads(request.body.decode("utf-8"))
                except ValueError:
                    return JsonResponse({"error": "Invalid JSON body."}, status=400)
            try:
                return fn(request, *args, **kwargs)
            except FsError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            except Http404:
                return JsonResponse({"error": "Not found."}, status=404)

        return wrapper

    return deco


def ws_fs(ws_id):
    ws = get_object_or_404(Workspace, pk=ws_id)
    return ws, WorkspaceFS(folders=ws.get_folders())


def workspace_dict(ws):
    folders = ws.get_folders()
    # Show the real first folder. The stored path can carry a #2 suffix when
    # another workspace already uses that folder as its lookup key.
    shown = folders[0]["path"] if folders else ws.path
    return {
        "id": ws.pk,
        "name": ws.name,
        "path": shown,
        "folders": folders,
    }


def default_agent():
    return Agent.objects.filter(is_default=True).first() or Agent.objects.first()


# ---------------------------------------------------------------------------
# pages / bootstrap
# ---------------------------------------------------------------------------
def _static_version():
    """Changes whenever the UI files change, so browsers never run a stale cached app.js/app.css."""
    base = Path(__file__).resolve().parent / "static" / "editor"
    stamps = [int((base / n).stat().st_mtime) for n in ("app.js", "app.css") if (base / n).exists()]
    return max(stamps) if stamps else 0


@never_cache
@ensure_csrf_cookie
def index(request):
    return render(request, "editor/index.html", {"static_version": _static_version()})


@api(["GET"])
def bootstrap(request):
    return JsonResponse({
        "workspaces": [workspace_dict(w) for w in Workspace.objects.all()],
        "agents": [a.to_dict() for a in Agent.objects.all()],
        "terminal_enabled": getattr(settings, "PSEDITOR_ALLOW_TERMINAL", False),
        "ocr_endpoint": ocr.endpoint(),
        "pty": ptyterm.available(),
        "platform": sys.platform,
        "home": str(Path.home()),
    })


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------
def _resolve_folder_path(raw, create=False):
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise FsError("Folder path is required.")
    path = Path(raw).expanduser()
    if not path.exists() and create:
        path.mkdir(parents=True)
    if not path.is_dir():
        raise FsError("That folder does not exist.")
    return path.resolve()


@api(["POST"])
def workspaces(request):
    data = request.json
    path = _resolve_folder_path(data.get("path"), create=data.get("create"))
    name = data.get("name") or folder_label(path)
    ws, created = Workspace.objects.get_or_create(
        path=str(path), defaults={"name": name}
    )
    if created or not ws.folders_json.strip():
        ws.set_folders([{"name": name, "path": str(path)}])
    ws.save()  # bump last_opened
    return JsonResponse(workspace_dict(ws))


@api(["DELETE"])
def workspace_detail(request, ws_id):
    get_object_or_404(Workspace, pk=ws_id).delete()  # files on disk are untouched
    return JsonResponse({"ok": True})


@api(["POST", "DELETE"])
def workspace_folders(request, ws_id):
    """Add or remove a folder in a multi-root workspace."""
    ws = get_object_or_404(Workspace, pk=ws_id)
    if request.method == "POST":
        path = _resolve_folder_path(request.json.get("path"), create=request.json.get("create"))
        try:
            ws.add_folder(str(path), name=request.json.get("name"))
        except ValueError as exc:
            raise FsError(str(exc))
        ws.save()
        return JsonResponse(workspace_dict(ws))
    # DELETE
    try:
        ws.remove_folder(path=request.json.get("path"), name=request.json.get("name"))
    except ValueError as exc:
        raise FsError(str(exc))
    ws.save()
    return JsonResponse(workspace_dict(ws))


def _windows_drives():
    """Return present drive roots via GetLogicalDrives (avoids os.path.exists hangs)."""
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{chr(ord('A') + i)}:\\" for i in range(26) if mask & (1 << i)]


@api(["POST"])
def browse(request):
    """Server-side folder picker."""
    raw = (request.json.get("path") or "").strip()
    if not raw:
        if sys.platform == "win32":
            drives = _windows_drives()
            return JsonResponse({"path": "", "parent": None,
                                 "dirs": [{"name": d, "path": d} for d in drives],
                                 "home": str(Path.home())})
        raw = str(Path.home())
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise FsError("Not a folder.")
    p = p.resolve()
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda x: x.name.lower()):
            try:
                if e.is_dir() and not e.name.startswith("$"):
                    dirs.append({"name": e.name, "path": str(Path(e.path))})
            except OSError:
                continue
    except PermissionError:
        raise FsError("Permission denied.")
    parent = str(p.parent) if p.parent != p else ("" if sys.platform == "win32" else None)
    return JsonResponse({"path": str(p), "parent": parent, "dirs": dirs, "home": str(Path.home())})


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
@api(["GET"])
def tree(request, ws_id):
    _, fs = ws_fs(ws_id)
    return JsonResponse({"entries": fs.tree(request.GET.get("path", ""))})


@api(["GET"])
def file_list(request, ws_id):
    _, fs = ws_fs(ws_id)
    return JsonResponse({"files": fs.list_files(limit=5000)})


@api(["GET", "PUT", "POST", "DELETE", "PATCH"])
def file_view(request, ws_id):
    _, fs = ws_fs(ws_id)
    if request.method == "GET":
        path = request.GET.get("path", "")
        return JsonResponse({"path": path, "content": fs.read(path)})
    data = request.json
    path = data.get("path", request.GET.get("path", ""))
    if request.method == "PUT":
        fs.write(path, data.get("content", ""))
        return JsonResponse({"ok": True, "path": path, "abs": str(fs.resolve(path))})
    if request.method == "POST":
        if data.get("type") == "dir":
            fs.mkdir(path)
        else:
            fs.create_file(path)
        return JsonResponse({"ok": True, "path": path, "abs": str(fs.resolve(path))})
    if request.method == "PATCH":
        fs.rename(path, data.get("new_path", ""))
        return JsonResponse({"ok": True})
    fs.delete(path)
    return JsonResponse({"ok": True})


_PREVIEW_BRIDGE = r"""
<script>
(function () {
  if (window.__pseditor) return;
  const wsId = %d;
  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }
  async function api(path, method, body) {
    const o = {
      method: method || "GET",
      headers: { "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    };
    let url = "/api/ws/" + wsId + "/file";
    if (!method || method === "GET") {
      url += "?path=" + encodeURIComponent(path);
    } else {
      o.headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(body !== undefined ? body : { path: path });
    }
    const r = await fetch(url, o);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || (r.status + " " + r.statusText));
    return data;
  }
  window.__pseditor = {
    wsId,
    async read(path) { return (await api(path, "GET")).content; },
    async write(path, content) {
      return api(path, "PUT", { path: path, content: String(content ?? "") });
    },
  };
  document.documentElement.dataset.pseditorDisk = "1";
  console.info("[PSEditor] Disk bridge ready — window.__pseditor.write(path, content)");
})();
</script>
"""


@xframe_options_sameorigin
@ensure_csrf_cookie
def preview(request, ws_id):
    """Serve a workspace file in the browser; HTML gets a disk write bridge."""
    _, fs = ws_fs(ws_id)
    path = (request.GET.get("path") or "").strip()
    if not path:
        raise FsError("Missing path.")
    abs_path = fs.resolve(path)
    if not abs_path.is_file():
        raise FsError("File not found.")
    ctype, _ = mimetypes.guess_type(str(abs_path))
    ctype = ctype or "application/octet-stream"
    if ctype.startswith("text/html") or abs_path.suffix.lower() in (".html", ".htm"):
        try:
            html = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FsError(f"Could not read file: {exc}")
        bridge = _PREVIEW_BRIDGE % int(ws_id)
        m = re.search(r"(?i)</head>", html)
        if m:
            html = html[: m.start()] + bridge + html[m.start() :]
        else:
            html = bridge + html
        return HttpResponse(html, content_type="text/html; charset=utf-8")
    return FileResponse(abs_path.open("rb"), content_type=ctype)


@api(["GET", "PUT"])
def ocr_settings(request):
    """Read or update the OCR endpoint shown by the settings icon."""
    if request.method == "GET":
        return JsonResponse(ocr.public())
    d = request.json
    return JsonResponse(ocr.save(d.get("endpoint", ""), d.get("model", ""), d.get("api_key", "")))


@api(["POST"])
def ocr_test(request):
    """Try the OCR endpoint with a small image. Uses the form values, including an unsaved key."""
    d = request.json
    ok, message = ocr.test_connection(d.get("endpoint"), d.get("model"), d.get("api_key"))
    return JsonResponse({"ok": ok, "message": message})


@api(["POST"])
def ocr_view(request, ws_id):
    """Read text from a workspace image using PSEDITOR_OCR_ENDPOINT."""
    _, fs = ws_fs(ws_id)
    path = (request.json.get("path") or request.GET.get("path") or "").strip()
    if not path:
        raise FsError("Missing path.")
    return JsonResponse({"path": path, "text": ocr.read_text(fs, path)})


@api(["POST"])
def search(request, ws_id):
    _, fs = ws_fs(ws_id)
    d = request.json
    results = fs.search(d.get("query", ""), regex=bool(d.get("regex")), case=bool(d.get("case")),
                        glob=d.get("glob") or None)
    return JsonResponse({"results": results})


@api(["POST"])
def terminal(request, ws_id):
    """Integrated terminal: a persistent real shell (PowerShell / bash), one per workspace.

    Body: {"command": "..."}  run a command (empty = just return the current prompt)
          {"interrupt": true}  Ctrl+C: kill the running command (the shell restarts)
          {"reset": true}      throw the shell away and start a fresh one
    Not confined to the workspace folders (the agents' run_command tool still is).
    """
    if not getattr(settings, "PSEDITOR_ALLOW_TERMINAL", False):
        raise FsError("Terminal is disabled (PSEDITOR_ALLOW_TERMINAL = False).")
    _, fs = ws_fs(ws_id)
    d = request.json
    if d.get("interrupt"):
        shell.interrupt(ws_id)
        return JsonResponse({"ok": True})
    if d.get("reset"):
        shell.reset(ws_id)
    session = shell.get_session(ws_id, fs.root)
    command = (d.get("command") or "").strip()
    return JsonResponse(session.run(command) if command else session.state())


# -- real terminal (PTY): the browser renders it with xterm.js ---------------------------------
def _pty_allowed():
    if not getattr(settings, "PSEDITOR_ALLOW_TERMINAL", False):
        raise FsError("Terminal is disabled (PSEDITOR_ALLOW_TERMINAL = False).")


def _pty_for(ws_id, sid):
    """The workspace's live PTY if ``sid`` still matches it, else None (the page then reopens it)."""
    s = ptyterm.get_session(ws_id)
    return s if s is not None and s.sid == sid else None


@api(["POST"])
def pty_open(request, ws_id):
    """Start (or reattach to) the workspace's terminal. Body: {cols, rows, restart}."""
    _pty_allowed()
    _, fs = ws_fs(ws_id)
    d = request.json
    s = ptyterm.open_session(ws_id, fs.root, cols=int(d.get("cols") or 100), rows=int(d.get("rows") or 24),
                             restart=bool(d.get("restart")))
    if d.get("cols") and d.get("rows"):
        s.resize(d["cols"], d["rows"])
    return JsonResponse({"sid": s.sid})


@api(["POST"])
def pty_input(request, ws_id):
    _pty_allowed()
    s = _pty_for(ws_id, request.json.get("sid"))
    if s is None:
        return JsonResponse({"stale": True}, status=409)
    s.write(request.json.get("data") or "")
    return JsonResponse({"ok": True})


@api(["POST"])
def pty_resize(request, ws_id):
    _pty_allowed()
    s = _pty_for(ws_id, request.json.get("sid"))
    if s is None:
        return JsonResponse({"stale": True}, status=409)
    s.resize(request.json.get("cols") or 100, request.json.get("rows") or 24)
    return JsonResponse({"ok": True})


@api(["GET"])
def pty_read(request, ws_id):
    """Long-poll for output from offset ``since``. Returns {sid, data, end, alive, reset}."""
    _pty_allowed()
    s = _pty_for(ws_id, request.GET.get("sid"))
    if s is None:
        return JsonResponse({"stale": True}, status=409)
    try:
        since = max(0, int(request.GET.get("since", 0)))
        wait = min(25.0, float(request.GET.get("wait", 0)))
    except ValueError:
        raise FsError("Bad since/wait.")
    return JsonResponse(s.read(since, wait))


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------
def _apply_agent_fields(agent, d, creating):
    for f in ("name", "provider", "base_url", "model", "command", "system_prompt", "extra_headers"):
        if f in d:
            setattr(agent, f, (d[f] or "").strip() if f != "system_prompt" else (d[f] or ""))
    if d.get("api_key"):  # blank on edit = keep the stored key
        agent.api_key = d["api_key"].strip()
    if d.get("clear_key"):
        agent.api_key = ""
    if "temperature" in d:
        try:
            agent.temperature = float(d["temperature"]) if d["temperature"] not in (None, "") else None
        except (TypeError, ValueError):
            raise FsError("Temperature must be a number.")
    if agent.provider not in dict(Agent.PROVIDERS):
        raise FsError("Unknown provider.")
    if not agent.name:
        raise FsError("Name is required.")
    if agent.extra_headers.strip():
        try:
            if not isinstance(json.loads(agent.extra_headers), dict):
                raise ValueError
        except ValueError:
            raise FsError("Extra headers must be a JSON object.")
    if agent.provider == Agent.OPENAI and not agent.model:
        raise FsError("Model is required for OpenAI-compatible agents.")
    if agent.provider == Agent.CLI and not agent.command:
        raise FsError("Command is required for command-line agents.")
    if agent.provider == Agent.WEBHOOK and not agent.base_url:
        raise FsError("URL is required for webhook agents.")
    if agent.provider == Agent.OPENAI:  # fix the classic mistake: OpenRouter key + OpenAI URL
        agent.base_url = agent_mod.resolve_openai_base(agent.base_url, agent.api_key) \
            if agent.api_key.startswith("sk-or-") else agent.base_url
    make_default = bool(d.get("is_default")) or (creating and not Agent.objects.exists())
    agent.is_default = make_default
    clash = Agent.objects.filter(name=agent.name).exclude(pk=agent.pk)
    if clash.exists():
        raise FsError("An agent with that name already exists.")
    agent.save()
    if make_default:
        Agent.objects.exclude(pk=agent.pk).update(is_default=False)


@api(["GET", "POST"])
def agents(request):
    if request.method == "GET":
        return JsonResponse({"agents": [a.to_dict() for a in Agent.objects.all()]})
    agent = Agent()
    _apply_agent_fields(agent, request.json, creating=True)
    return JsonResponse(agent.to_dict())


@api(["PUT", "DELETE"])
def agent_detail(request, agent_id):
    agent = get_object_or_404(Agent, pk=agent_id)
    if request.method == "DELETE":
        agent.delete()
        return JsonResponse({"ok": True})
    _apply_agent_fields(agent, request.json, creating=False)
    return JsonResponse(agent.to_dict())


@api(["POST"])
def agent_default(request, agent_id):
    """Save the agent picked in the chat panel: it becomes the default agent."""
    agent = get_object_or_404(Agent, pk=agent_id)
    Agent.objects.exclude(pk=agent.pk).update(is_default=False)
    agent.is_default = True
    agent.save(update_fields=["is_default"])
    return JsonResponse(agent.to_dict())


@api(["POST"])
def agent_model(request, agent_id):
    """Save the model picked in the chat panel as the agent's model: {"model": "..."}."""
    agent = get_object_or_404(Agent, pk=agent_id)
    if agent.provider not in (Agent.OPENAI, Agent.ANTHROPIC):
        raise FsError("This agent type does not use a model.")
    model = (request.json.get("model") or "").strip()
    if not model:
        raise FsError("Model is required.")
    if len(model) > Agent._meta.get_field("model").max_length:
        raise FsError("Model name is too long.")
    agent.model = model
    agent.save(update_fields=["model"])
    return JsonResponse(agent.to_dict())


@api(["POST"])
def models_list(request):
    """List models for an (possibly unsaved) agent configuration."""
    d = request.json
    saved = Agent.objects.filter(pk=d.get("agent_id")).first() if d.get("agent_id") else None
    provider = d.get("provider") or (saved.provider if saved else Agent.OPENAI)
    base_url = (d.get("base_url") or "").strip() or (saved.base_url if saved else "")
    api_key = (d.get("api_key") or "").strip() or (saved.api_key if saved else "")
    try:
        models = agent_mod.list_models(provider, base_url, api_key, saved.headers() if saved else None)
    except agent_mod.AgentError as exc:
        raise FsError(str(exc))
    return JsonResponse({"models": models})


@api(["POST"])
def agent_test(request, agent_id):
    agent = get_object_or_404(Agent, pk=agent_id)
    ok, message = agent_mod.test_agent(agent)
    return JsonResponse({"ok": ok, "message": message})


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
def session_dict(s):
    return {"id": s.pk, "title": s.title, "agent_id": s.agent_id, "updated": s.updated.isoformat()}


@api(["GET", "POST"])
def sessions(request, ws_id):
    ws, _ = ws_fs(ws_id)
    if request.method == "POST":
        s = ChatSession.objects.create(workspace=ws, agent=default_agent())
        return JsonResponse(session_dict(s))
    return JsonResponse({"sessions": [session_dict(s) for s in ws.sessions.all()[:50]]})


@api(["GET", "DELETE"])
def session_detail(request, session_id):
    s = get_object_or_404(ChatSession, pk=session_id)
    if request.method == "DELETE":
        s.delete()
        return JsonResponse({"ok": True})
    out = []
    for m in s.messages.all():
        d = m.data()
        if m.role == "user":
            changed = list(json.loads(m.checkpoint).keys()) if m.checkpoint else []
            item = {"id": m.pk, "role": "user", "text": d.get("display", d["content"]),
                    "changed": changed}
            if d.get("attachments"):
                item["attachments"] = d["attachments"]
            out.append(item)
        elif m.role == "assistant":
            item = {"id": m.pk, "role": "assistant", "text": d.get("content", ""),
                    "tool_calls": [{"id": t["id"], "name": t["name"], "arguments": t["arguments"]}
                                   for t in d.get("tool_calls", [])]}
            if d.get("thinking"):
                item["thinking"] = d["thinking"]
            out.append(item)
        else:
            out.append({"id": m.pk, "role": "tool", "name": d.get("name", ""),
                        "tool_call_id": d.get("tool_call_id", ""),
                        "text": d.get("content", "")[:1500]})
    return JsonResponse({"session": session_dict(s), "messages": out})


@api(["POST"])
def chat(request, session_id):
    s = get_object_or_404(ChatSession, pk=session_id)
    d = request.json
    text = (d.get("message") or "").strip()
    prepared = prepare_attachments(d.get("attachments") or [])
    if not text and not prepared.meta:
        raise FsError("Message is empty.")
    agent = None
    if d.get("agent_id"):
        agent = Agent.objects.filter(pk=d["agent_id"]).first()
    agent = agent or s.agent or default_agent()
    if agent is None:
        raise FsError("No agent configured. Open Settings and add an agent first.")
    if agent.provider == Agent.OPENAI and not agent.api_key and "openrouter.ai" in agent.base_url:
        raise FsError(f"Agent '{agent.name}' has no OpenRouter API key. Click Agents and paste your key "
                      "(get one at https://openrouter.ai/keys).")
    model = (d.get("model") or "").strip()
    if model and agent.provider in (Agent.OPENAI, Agent.ANTHROPIC):
        agent.model = model  # per-request override; the saved agent is not modified
    fs = WorkspaceFS(folders=s.workspace.get_folders())
    active = d.get("active_file")
    thinking = agent_mod.normalize_thinking(d.get("thinking"))

    def stream():
        for event in agent_mod.run_turn(s, agent, fs, text, active, prepared=prepared, thinking=thinking):
            yield json.dumps(event) + "\n"

    resp = StreamingHttpResponse(stream(), content_type="application/x-ndjson")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@api(["POST"])
def approval_decide(request, approval_id):
    """The user's answer to an agent action waiting for approval: {"decision": "allow"|"deny", "reason": ""}."""
    d = request.json
    decision = d.get("decision")
    if decision not in (approvals.ALLOW, approvals.DENY):
        raise FsError("decision must be 'allow' or 'deny'.")
    if not approvals.decide(approval_id, decision, d.get("reason") or ""):
        return JsonResponse({"error": "This request is no longer waiting for approval "
                                      "(it timed out or the chat was stopped)."}, status=409)
    return JsonResponse({"ok": True})


@api(["POST"])
def session_stop(request, session_id):
    """Stop button: release any action of this chat that is waiting for approval (it is not run)."""
    get_object_or_404(ChatSession, pk=session_id)
    return JsonResponse({"released": approvals.cancel_session(session_id)})


@api(["POST"])
def message_revert(request, message_id):
    row = get_object_or_404(ChatMessage, pk=message_id)
    if not row.checkpoint:
        raise FsError("Nothing to revert.")
    fs = WorkspaceFS(folders=row.session.workspace.get_folders())
    touched = revert(fs, json.loads(row.checkpoint))
    row.checkpoint = ""
    row.save(update_fields=["checkpoint"])
    return JsonResponse({"reverted": touched})
