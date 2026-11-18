"""Run PSEditor in a native desktop window (no web browser needed).

Starts the Django server on 127.0.0.1 in a background thread and shows the UI
in an embedded window (WebView2 on Windows, WKWebView on macOS) via pywebview.
Closing the window stops the server.

Usage:  python desktop.py            (run.bat / run_mac.command do this for you)
Env:    PSEDITOR_PORT=8800           preferred port (falls back to a free one)
"""

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pseditor.settings")

HOST = "127.0.0.1"
LOG_PATH = BASE_DIR / "desktop.log"
ICON_PATH = BASE_DIR / "pseditor.ico"


def _set_windows_app_id():
    """Detach from pythonw.exe's taskbar group so Windows uses our own window icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pseditor.desktop.app")
    except Exception:
        pass


def _setup_logging():
    """pythonw has no console: send output to desktop.log so errors are visible."""
    if sys.stdout is None or sys.stderr is None:
        log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log


def _pick_port():
    preferred = int(os.environ.get("PSEDITOR_PORT", "8800"))
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("No free local port available.")


def _start_server(port):
    import django
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler
    from django.core.wsgi import get_wsgi_application

    django.setup()
    from django.core.management import call_command

    call_command("migrate", interactive=False, verbosity=0)

    app = StaticFilesHandler(get_wsgi_application())
    server = ThreadedWSGIServer((HOST, port), WSGIRequestHandler)
    server.daemon_threads = True
    server.set_app(app)
    threading.Thread(target=server.serve_forever, daemon=True, name="pseditor-http").start()
    return server


def _wait_ready(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


class DesktopApi:
    """Exposed to the page as window.pywebview.api (see openPreview in app.js)."""

    def __init__(self, base_url):
        self.base_url = base_url

    def open_window(self, path):
        import webview

        if not isinstance(path, str) or not path.startswith("/"):
            return False
        webview.create_window(
            "PSEditor preview", self.base_url + path, width=1000, height=720, text_select=True,
        )
        return True


def _allow_microphone(sender, args):
    """The chat Listen button needs the microphone. Allow it without a second prompt."""
    try:
        kind = str(args.PermissionKind)
        if "Microphone" not in kind:
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState

        args.State = CoreWebView2PermissionState.Allow
    except Exception:
        pass


def _enable_edit_menu():
    """Right-click Copy, Paste, Undo and Redo stay off unless debug mode is on.

    Browser shortcuts (reload, print, find) stay off so they do not steal Ctrl+P and Ctrl+F.
    """
    try:
        from webview.platforms.edgechromium import EdgeChrome
    except Exception:
        return
    original = EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        original(self, sender, args)
        try:
            sender.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True
        except Exception:
            pass
        try:
            sender.CoreWebView2.PermissionRequested += _allow_microphone
        except Exception:
            pass

    EdgeChrome.on_webview_ready = on_webview_ready


def main():
    _setup_logging()
    _set_windows_app_id()
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run:  pip install pywebview", file=sys.stderr)
        return 1
    _enable_edit_menu()

    port = _pick_port()
    base_url = f"http://{HOST}:{port}"
    server = _start_server(port)
    if not _wait_ready(base_url + "/"):
        print("The PSEditor server did not start in time.", file=sys.stderr)
        return 1

    # Keep every link inside the app instead of handing it to the default browser.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    webview.create_window(
        "PSEditor",
        base_url + "/",
        width=1400,
        height=900,
        min_size=(900, 600),
        text_select=True,
        js_api=DesktopApi(base_url),
    )
    icon = str(ICON_PATH) if ICON_PATH.is_file() else None
    webview.start(icon=icon)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
