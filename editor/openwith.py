"""Ask the operating system to open a file with another program."""
import subprocess
import sys

# Shown by a short-lived process so the editor request is not stuck on the dialog.
_WIN_DIALOG = r"""
import ctypes, sys
from ctypes import wintypes

class OPENASINFO(ctypes.Structure):
    _fields_ = [
        ("pcszFile", wintypes.LPCWSTR),
        ("pcszClass", wintypes.LPCWSTR),
        ("oaifInFlags", wintypes.DWORD),
    ]

ctypes.windll.ole32.CoInitialize(None)
shell32 = ctypes.windll.shell32
shell32.SHOpenWithDialog.argtypes = [wintypes.HWND, ctypes.POINTER(OPENASINFO)]
shell32.SHOpenWithDialog.restype = ctypes.c_long
# Let the user pick an app, remember it for this extension, and open the file.
info = OPENASINFO(sys.argv[1], None, 0x1 | 0x2 | 0x4)
shell32.SHOpenWithDialog(None, ctypes.byref(info))
"""


def launch(path):
    """Open the system Open With dialog (Windows) or the default app (macOS and Linux)."""
    target = str(path)
    if sys.platform == "win32":
        cmd = [sys.executable, "-c", _WIN_DIALOG, target]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif sys.platform == "darwin":
        cmd = ["open", target]
        flags = 0
    else:
        cmd = ["xdg-open", target]
        flags = 0
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError as exc:
        from .fsops import FsError
        raise FsError(f"Could not open this file: {exc}")
