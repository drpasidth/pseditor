"""A real terminal (pseudo-terminal) for the integrated terminal.

Unlike ``shell.py`` (commands piped into a hidden shell), this attaches the shell to a PTY
(Windows ConPTY through ``pywinpty``; the stdlib ``pty`` module elsewhere). Programs therefore see a real
terminal: ``input()``/password prompts, ``manage.py createsuperuser``, the ``python`` REPL, colours,
arrow-key history, Ctrl+C and so on all work. The browser renders it with xterm.js.

Transport is plain HTTP (the dev server has no websockets): the browser POSTs keystrokes and long-polls
for output. Output is kept in a bounded buffer addressed by offset, so a page reload can replay the screen.

It is a real shell and is not confined to the workspace folders (the AI agents' tools still are).
"""
import atexit
import base64
import os
import subprocess
import sys
import threading
import uuid

from .fsops import FsError
from .shell import _clean_env

IS_WIN = sys.platform == "win32"
MAX_BUFFER = 1_000_000   # characters of scrollback kept for replay
KEEP_AFTER_TRIM = 600_000

if IS_WIN:
    try:
        from winpty import PtyProcess
    except ImportError:  # pragma: no cover - depends on the environment
        PtyProcess = None


def available():
    return PtyProcess is not None if IS_WIN else hasattr(os, "fork")


def unavailable_reason():
    if IS_WIN:
        return "The real terminal needs the 'pywinpty' package: run  pip install pywinpty"
    return "The real terminal needs a POSIX system with pty support."


# PowerShell start-up: UTF-8, a short prompt (`E:\path> `; an activated venv adds "(venv) " itself), and
# a hook that makes cmd/bash-style venv commands work in PowerShell, where they would silently do nothing.
_POWERSHELL_INIT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Console]::InputEncoding  = [Text.Encoding]::UTF8
function global:prompt { $executionContext.SessionState.Path.CurrentLocation.Path + '> ' }
$ExecutionContext.InvokeCommand.PreCommandLookupAction = {
    param($name, $e)
    if ($name -notmatch '(?i)^(.*[\\/])?(de)?activate(\.bat)?$') { return }
    $dir = $Matches[1]; $isDe = [bool]$Matches[2]
    if ($isDe) {
        # `deactivate` alone is the function Activate.ps1 defines; only map `...\deactivate.bat`
        if ($name -match '(?i)\.bat$') { $e.CommandScriptBlock = { deactivate }; $e.StopSearch = $true }
        return
    }
    $base = if ($dir) { $dir } else { '.\' }
    $ps1 = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath((Join-Path $base 'Activate.ps1'))
    if (Test-Path -LiteralPath $ps1) {
        $e.CommandScriptBlock = { . $ps1 }.GetNewClosure()
        $e.StopSearch = $true
    }
}
function global:call   { $rest = @($args | Select-Object -Skip 1); & $args[0] @rest }
function global:source { $rest = @($args | Select-Object -Skip 1); & $args[0] @rest }
"""


def _powershell_args():
    encoded = base64.b64encode(_POWERSHELL_INIT.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-NoExit", "-EncodedCommand", encoded]


_job = None


def _bind_to_server_lifetime(pid):
    """Windows: put the shell in a job object that is killed when this server process dies for any reason
    (closed console window, crash, kill), so abrupt exits do not leave orphaned shells. Best effort."""
    global _job
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _job is None:
            class _Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

            class _Extended(ctypes.Structure):
                _fields_ = [("Basic", _Basic), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            info = _Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            _job = job
        handle = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))  # SET_QUOTA | TERMINATE
        if handle:
            k32.AssignProcessToJobObject(_job, handle)
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - purely a safety net
        pass


class _WinBackend:
    def __init__(self, cwd, cols, rows, env):
        self.p = PtyProcess.spawn(_powershell_args(), cwd=cwd, env=env, dimensions=(rows, cols))
        _bind_to_server_lifetime(self.p.pid)

    def read(self):
        return self.p.read(65536)

    def write(self, data):
        self.p.write(data)

    def resize(self, cols, rows):
        self.p.setwinsize(rows, cols)

    def alive(self):
        return self.p.isalive()

    def kill(self):
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
        except Exception:
            pass
        try:
            self.p.terminate(force=True)
        except Exception:
            pass


class _PosixBackend:  # pragma: no cover - not exercised on Windows
    def __init__(self, cwd, cols, rows, env):
        import codecs
        import pty
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.master, slave = pty.openpty()
        env = dict(env, TERM="xterm-256color", BASH_SILENCE_DEPRECATION_WARNING="1")
        env.setdefault("PS1", r"\w$ ")
        self.proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-i"], cwd=cwd, env=env, stdin=slave, stdout=slave,
            stderr=slave, close_fds=True, preexec_fn=os.setsid,
        )
        os.close(slave)
        self.resize(cols, rows)

    def read(self):
        data = os.read(self.master, 65536)
        if not data:
            raise EOFError
        return self._decoder.decode(data)

    def write(self, data):
        os.write(self.master, data.encode("utf-8"))

    def resize(self, cols, rows):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        import signal
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass


class PtySession:
    def __init__(self, cwd, cols=100, rows=24):
        self.sid = uuid.uuid4().hex
        self.cond = threading.Condition()
        self.buf = ""
        self.base = 0        # offset of buf[0]
        self.alive = True
        self.write_lock = threading.Lock()
        env = _clean_env()
        backend = _WinBackend if IS_WIN else _PosixBackend
        try:
            self.backend = backend(str(cwd), int(cols), int(rows), env)
        except Exception as exc:  # noqa: BLE001
            raise FsError(f"Could not start the terminal: {exc}")
        threading.Thread(target=self._reader, daemon=True).start()

    @property
    def end(self):
        return self.base + len(self.buf)

    def _push(self, text):
        with self.cond:
            self.buf += text
            if len(self.buf) > MAX_BUFFER:
                drop = len(self.buf) - KEEP_AFTER_TRIM
                self.buf = self.buf[drop:]
                self.base += drop
            self.cond.notify_all()

    def _reader(self):
        import time
        try:
            while True:
                try:
                    chunk = self.backend.read()
                except EOFError:
                    break
                except OSError:
                    break
                if chunk:
                    self._push(chunk)
                elif not self.backend.alive():
                    break
                else:
                    time.sleep(0.02)
        finally:
            with self.cond:
                self.alive = False
            self._push("\r\n\x1b[2m[process exited - press any key to start a new terminal]\x1b[0m\r\n")

    # -- api used by the views --------------------------------------------
    def read(self, since, wait=0.0):
        """Output from offset ``since``; long-polls up to ``wait`` seconds for new data."""
        import time
        deadline = time.monotonic() + max(0.0, wait)
        with self.cond:
            while self.end <= since and self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            reset = since < self.base
            start = max(since, self.base) - self.base
            return {"sid": self.sid, "data": self.buf[start:], "end": self.end,
                    "alive": self.alive, "reset": reset}

    def write(self, data):
        if not self.alive:
            return
        with self.write_lock:
            try:
                self.backend.write(data)
            except Exception:  # noqa: BLE001 - the reader notices the process is gone
                pass

    def resize(self, cols, rows):
        try:
            self.backend.resize(max(2, int(cols)), max(2, int(rows)))
        except Exception:  # noqa: BLE001
            pass

    def kill(self):
        self.backend.kill()


_sessions = {}
_lock = threading.Lock()


def open_session(key, cwd, cols=100, rows=24, restart=False):
    """The workspace's live terminal, creating (or, with restart, replacing) it as needed."""
    if not available():
        raise FsError(unavailable_reason())
    with _lock:
        s = _sessions.get(key)
        if s is not None and (restart or not s.alive):
            s.kill()
            s = None
        if s is None:
            s = _sessions[key] = PtySession(cwd, cols, rows)
        return s


def get_session(key):
    with _lock:
        return _sessions.get(key)


def kill_session(key):
    with _lock:
        s = _sessions.pop(key, None)
    if s is not None:
        s.kill()


@atexit.register
def _shutdown():
    for s in list(_sessions.values()):
        try:
            s.kill()
        except Exception:  # noqa: BLE001
            pass
