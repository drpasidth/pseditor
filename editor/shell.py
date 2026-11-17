"""Persistent shell sessions for the integrated terminal.

One long-lived shell process (PowerShell on Windows, bash elsewhere) is kept per workspace, so
state survives between commands: current folder, environment variables, an activated virtualenv,
functions and variables. Commands are sent over stdin; a random marker line printed after each
command tells us it finished and reports the exit code, the current folder and the venv name.

This is a *real* shell, not confined to the workspace folders. It is only for commands the user
types. The AI agents' ``run_command`` tool still uses the confined ``WorkspaceFS.run``.
Interactive programs that read stdin (python REPL, prompts) are not supported - press Ctrl+C.
"""
import atexit
import base64
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from django.conf import settings

from .fsops import FsError

IS_WIN = sys.platform == "win32"
MAX_OUTPUT = 1_000_000  # bytes kept per command; the rest is discarded


class ShellError(FsError):
    pass


def _timeout():
    return getattr(settings, "PSEDITOR_TERMINAL_TIMEOUT", 120)


# Variables the editor's own Django process exports that must not leak into the shells and
# terminals it starts. Otherwise `manage.py` in *any other* Django project in the workspace
# picks up PSEditor's settings module and dies with `No module named 'pseditor'`.
_OWN_DJANGO_ENV = (
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_ALLOWED_HOSTS",
)


def _clean_env():
    """Environment for the shell: like the server's, minus the server's own virtualenv.

    Also drops the editor's own ``DJANGO_*`` variables, so a second Django project opened in
    the workspace is not forced to import ``pseditor.settings``.
    """
    env = os.environ.copy()
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    for name in _OWN_DJANGO_ENV:
        env.pop(name, None)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        env.pop("VIRTUAL_ENV", None)
        env.pop("VIRTUAL_ENV_PROMPT", None)
        bindir = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if IS_WIN else "bin")))
        key = "Path" if IS_WIN and "Path" in env else "PATH"
        parts = env.get(key, "").split(os.pathsep)
        env[key] = os.pathsep.join(p for p in parts if os.path.normcase(p.rstrip("\\/")) != bindir)
    return env


def _kill_tree(proc):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def _reader(proc, q):
    try:
        for line in iter(proc.stdout.readline, b""):
            q.put(line)
    except Exception:
        pass
    finally:
        q.put(None)  # EOF: the shell exited


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _venv_prefix(prompt, venv_path):
    prompt = (prompt or "").strip()
    if prompt:
        return prompt if prompt.startswith("(") else f"({prompt})"
    if venv_path:
        return f"({Path(venv_path).name})"
    return ""


class ShellSession:
    def __init__(self, cwd):
        self.lock = threading.Lock()
        self.initial_cwd = str(cwd)
        self.cwd = str(cwd)
        self.venv = ""
        self.proc = None
        self.q = None
        self.marker = ""
        self.interrupted = False
        self._start()

    # -- process -----------------------------------------------------------
    def _start(self):
        self.marker = f"<<PSE:{uuid.uuid4().hex[:12]}>>"
        cwd = self.cwd if Path(self.cwd).is_dir() else self.initial_cwd
        if IS_WIN:
            args = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", "-"]
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            args = ["bash", "--noprofile", "--norc"]
            flags = 0
        try:
            self.proc = subprocess.Popen(
                args, cwd=cwd, env=_clean_env(), creationflags=flags,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise ShellError(f"Could not start the shell: {exc}")
        self.q = queue.Queue()
        threading.Thread(target=_reader, args=(self.proc, self.q), daemon=True).start()
        self.venv = ""
        if IS_WIN:
            q = cwd.replace("'", "''")
            self._send(
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
                "$ProgressPreference='SilentlyContinue'; "
                # cmd-style bare `activate` (after `cd venv\\Scripts`): run the Activate.ps1 in the current folder
                "function global:activate { $s = Join-Path (Get-Location).Path 'Activate.ps1'; "
                "if (Test-Path -LiteralPath $s) { . $s } "
                "else { Write-Error 'activate: no Activate.ps1 in this folder. Use venv\\Scripts\\activate' } }; "
                f"Set-Location -LiteralPath '{q}'"
            )
        _, _, state = self._collect(time.monotonic() + 30, self._script(""))  # handshake: learn cwd
        if state:
            self.kill()
            raise ShellError("The shell did not start correctly.")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc is not None:
            _kill_tree(self.proc)

    def _send(self, text):
        self.proc.stdin.write((text + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    # -- protocol ----------------------------------------------------------
    def _script(self, command):
        """One line that runs ``command`` in the session's scope, then prints the marker."""
        m = self.marker
        if IS_WIN:
            run = ""
            if command:
                run = (
                    "try { Invoke-Expression ([Text.Encoding]::UTF8.GetString("
                    "[Convert]::FromBase64String('" + _b64(command) + "'))) "
                    "| Out-String -Width 300 -Stream } "
                    "catch { $_.Exception.Message }; "
                )
            return (
                "$global:LASTEXITCODE=0; $__f=$null; if($Error.Count -gt 0){$__f=$Error[0]}; "
                + run +
                "$__c=$LASTEXITCODE; if(-not $__c -and $Error.Count -gt 0 -and "
                "-not [object]::ReferenceEquals($Error[0],$__f)){$__c=1}; "
                f"Write-Output ('{m}' + [string]$__c + '|' + $env:VIRTUAL_ENV_PROMPT + '|' "
                "+ $env:VIRTUAL_ENV + '|' + (Get-Location).Path)"
            )
        run = f'eval "$(printf %s \'{_b64(command)}\' | base64 -d)"; ' if command else ""
        return (
            run + '__c=$?; printf \'\\n%s%s|%s|%s|%s\\n\' '
            f"'{m}' \"$__c\" \"$VIRTUAL_ENV_PROMPT\" \"$VIRTUAL_ENV\" \"$PWD\""
        )

    def _collect(self, deadline, script):
        """Send ``script`` and read until the marker. Returns (output, exit_code, finished)."""
        try:
            self._send(script)
        except OSError:
            return "", 1, False
        out, size, truncated = [], 0, False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "".join(out), 124, "timeout"
            try:
                raw = self.q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if raw is None:
                return "".join(out), 1, "exited"
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("\ufeff"):
                line = line[1:]
            idx = line.find(self.marker)
            if idx >= 0:
                before = line[:idx]
                if before.strip():
                    out.append(before.rstrip("\r\n") + "\n")
                fields = line[idx + len(self.marker):].rstrip("\r\n").split("|", 3)
                fields += [""] * (4 - len(fields))
                try:
                    code = int(fields[0])
                except ValueError:
                    code = 0
                self.venv = _venv_prefix(fields[1], fields[2])
                if fields[3]:
                    self.cwd = fields[3]
                return "".join(out), code, ""
            size += len(raw)
            if size <= MAX_OUTPUT:
                out.append(line.rstrip() + "\n")  # drops the padding PowerShell adds to wide tables
            elif not truncated:
                truncated = True
                out.append("[output truncated]\n")

    # -- public ------------------------------------------------------------
    def prompt(self):
        return (self.venv + " " if self.venv else "") + self.cwd + ">"

    def _result(self, output="", code=0):
        return {"output": output, "exit_code": code, "cwd": self.cwd,
                "venv": self.venv, "prompt": self.prompt()}

    def run(self, command, timeout=None):
        timeout = timeout or _timeout()
        with self.lock:
            notice = ""
            if not self.alive():
                self._start()
                notice = "[shell was restarted]\n"
            command = rewrite_command(command, self.cwd)
            out, code, state = self._collect(time.monotonic() + timeout, self._script(command))
            if state:
                interrupted, self.interrupted = self.interrupted, False
                self.kill()
                if state == "timeout":
                    out += f"[timed out after {timeout}s - shell restarted in {self.cwd}]\n"
                elif interrupted:
                    out += "^C\n[shell restarted in " + self.cwd + "; environment such as an activated venv was reset]\n"
                else:
                    out += "[shell exited - restarted]\n"
                self._start()
            return self._result(notice + out, code)

    def state(self):
        with self.lock:
            if not self.alive():
                self._start()
            return self._result()

    def interrupt(self):
        self.interrupted = True
        self.kill()


_PREFIX_RE = re.compile(r"^\s*(?:(?:call|source|\.|&)\s+)?", re.IGNORECASE)
_ACTIVATE_NAMES = {"activate": "Activate.ps1", "activate.bat": "Activate.ps1", "activate.ps1": "Activate.ps1"}


def _split_token(text):
    """First (possibly quoted) token of ``text`` and the remainder."""
    text = text.lstrip()
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        if end < 0:
            return None, ""
        return text[1:end], text[end + 1:]
    m = re.match(r"[^\s;]+", text)
    if not m:
        return None, ""
    return m.group(0), text[m.end():]


def rewrite_command(command, cwd):
    """Make cmd-style / bash-style venv commands work in PowerShell, where they'd silently do nothing.

    `venv\\Scripts\\activate`, `venv/scripts/activate`, `...\\activate.bat`, `call ...\\activate.bat`,
    `source venv/Scripts/activate` and a bare `activate` (inside Scripts) all become a dot-source of the
    folder's Activate.ps1, so the environment and the `(venv)` prompt persist in the shell session.
    `...\\deactivate.bat` becomes `deactivate`. Anything after a `;` is kept.
    """
    if not IS_WIN:
        return command
    prefix = _PREFIX_RE.match(command)
    token, rest = _split_token(command[prefix.end():])
    if not token or (rest.strip() and not rest.lstrip().startswith(";")):
        return command
    path = token.replace("/", "\\")
    name = path.rsplit("\\", 1)[-1].lower()
    if name == "deactivate.bat":
        return "deactivate" + rest
    if name not in _ACTIVATE_NAMES:
        return command
    folder = path[: len(path) - len(name)]
    script = Path(folder or ".") / _ACTIVATE_NAMES[name]
    if not script.is_absolute():
        script = Path(cwd) / script
    if not script.is_file():  # `activate` may be a different tool (e.g. conda); leave it alone
        return command
    return f". '{str(script).replace(chr(39), chr(39) * 2)}'" + rest


# ---------------------------------------------------------------------------
# registry: one shell per workspace
# ---------------------------------------------------------------------------
_sessions = {}
_registry_lock = threading.Lock()


def get_session(key, cwd):
    with _registry_lock:
        s = _sessions.get(key)
        if s is None:
            s = _sessions[key] = ShellSession(cwd)
        return s


def interrupt(key):
    """Ctrl+C: kill the shell; the running request returns and the next command starts a fresh one."""
    with _registry_lock:
        s = _sessions.get(key)
    if s is not None:
        s.interrupt()


def reset(key):
    with _registry_lock:
        s = _sessions.pop(key, None)
    if s is not None:
        s.kill()


@atexit.register
def _shutdown():
    for s in list(_sessions.values()):
        s.kill()
