"""Probe: does Ctrl+C interrupt a running command in the integrated terminal?

Runs the same sequence as editor/tests.py::test_pty_ctrl_c_interrupts_running_command
twice: once with the cleaned environment (current code) and once with the editor's
DJANGO_SETTINGS_MODULE forced back in, to see whether the environment change matters.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ["DJANGO_SETTINGS_MODULE"] = "pseditor.settings"
import django

django.setup()

from editor import ptyterm  # noqa: E402


def run_case(label, mutate):
    s = ptyterm.PtySession(ROOT)
    since = 0

    def pump(seconds):
        nonlocal since
        end = time.monotonic() + seconds
        out = ""
        while time.monotonic() < end:
            r = s.read(since, 0.3)
            out += r["data"]
            since = r["end"]
        return out

    pump(3)
    s.write("Start-Sleep 60\r")
    pump(1.5)
    s.write("\x03")
    time.sleep(1.0)
    after_c = pump(1.0)
    s.write("echo ('af' + 'ter')\r")
    out = pump(8)
    ok = "after" in out
    print(f"[{label}] alive={s.alive} after_ctrl_c_len={len(after_c)} "
          f"prompt_back={bool(after_c.strip())} result={'PASS' if ok else 'FAIL'}")
    try:
        s.end()
    except Exception:
        pass
    return ok


# 1. Current code: DJANGO_* stripped in _clean_env().
print("case 1: cleaned env (current code)")
run_case("cleaned", None)

# 2. Simulate the old behaviour: put the editor's settings variable back.
orig = ptyterm._clean_env


def with_django():
    env = orig()
    env["DJANGO_SETTINGS_MODULE"] = "pseditor.settings"
    return env


ptyterm._clean_env = with_django
print("case 2: DJANGO_SETTINGS_MODULE restored (old behaviour)")
run_case("with-django", None)
ptyterm._clean_env = orig
