import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import TestCase

from . import approvals, ptyterm, shell
from .models import Agent, Workspace


class FakeResp:
    def __init__(self, data, status=200, content=None):
        self._data, self.status_code = data, status
        self.text = json.dumps(data) if isinstance(data, (dict, list)) else ""
        self.content = b"" if content is None else content

    def json(self):
        return self._data


def stream_events(resp):
    body = b"".join(resp.streaming_content).decode()
    return [json.loads(l) for l in body.splitlines() if l.strip()]


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _pdf_bytes(text="Hello PDF"):
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 100 700 Td ({safe}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (220, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


def _mp4_bytes():
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2" + b"\x00" * 16


def _xlsx_bytes():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws["A1"], ws["B1"] = "Name", "Qty"
    ws["A2"], ws["B2"] = "Apple", 3
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xls_bytes():
    # A tiny .xls (sheet "Old": Item/Qty, Pen/4), compressed so the test does not need xlwt.
    import zlib
    blob = (
        "eNrtWE1oE0EU/maTND+26aamQlsooWDV2ot48VK3Clo8WFMvigiamhykMZWQSz1oteYoCJ4UL4VevFS9"
        "+IMKevMgVPQgCEKiR0+Cgoc265svu5L+HBrQUmW+Zb55++bNzNudNzM7+3YhUZl92F3FCuxHADU3ipYG"
        "nZIU9W9sSLnratHPI5Jcg38K0YgMZEsIz9rehPUY6vGuwsKD4Cth4LOk07iI0clCLrWBOEgfMkr7MCSs"
        "cFc0cXTRqw7yOfJW8n1aPicPU3ODPCS2FXUKC87owD4vik9afSyLQ7f7mHU+UrMHnXito/jKTVW3DeFA"
        "8XwmvzkLeoOtmIOM20iukCtm8hUkZQDn8MNNAd/9mfoyZfQbq1cQ/c/l+vAa+ltWEJiGe5YBXsYWvAjo"
        "kgCO5bOL6EGAN4CYHSnlLog4VpoSTucKMb0Yc/LayyZvG4O6VTiLdsoJhrYty/PivW/vjo6nnTPUTHPB"
        "ri/r27UncHFV15DKcZZYv3mAvJt8ja32UO4mJyUgJe9Pd3rC4RnaXGdpv/Szl3jv7GiQd4pc/jr2pLf8"
        "xdkl8vxI9VJy/oMziz7ZZrJSX18zGFSD6s5tjaeOnytvCfhE7lq1HEQs2/Pd9faudiwhRjFBrt/pt6M8"
        "e7WGvaK9bvGyFaM/CelZtkerA4/4eoYbNsgYDAwMDAwMDAwMDDY1lPeB750y5Pu9fowIe/91liTVzG+S"
        "/xbHMSlXSQ6mh1CQvIippuJnG0LKb0uts47/v1DjhPRexATG6cdE0/Erx0DV+Dzrrmj/uSnUbP+1Zvz8"
        "y/3/AjjPvns="
    )
    return zlib.decompress(base64.b64decode(blob))


def _docx_bytes():
    from docx import Document
    doc = Document()
    doc.add_paragraph("Hello Word")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Name"
    table.rows[0].cells[1].text = "Ada"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _doc_bytes(text):
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + text.encode("utf-16le")


class Human(threading.Thread):
    """Plays the user clicking Approve/Deny. Only touches the in-memory approval registry (no DB)."""

    def __init__(self, answers, check=None):
        super().__init__(daemon=True)
        self.answers = list(answers)   # "allow" | "deny" | ("deny", reason) | "stop"
        self.check = check             # called with (approval) at the moment the request is shown
        self.seen = []
        self.halt = False

    def run(self):
        done = set()
        while not self.halt:
            with approvals._lock:
                todo = [a for a in approvals._pending.values() if a.decision is None and a.id not in done]
            for a in todo:
                done.add(a.id)
                self.seen.append((a.name, a.arguments))
                if self.check:
                    self.check(a)
                ans = self.answers.pop(0) if self.answers else "deny"
                ans, reason = ans if isinstance(ans, tuple) else (ans, "")
                if ans == "stop":
                    approvals.cancel_session(a.session_id)
                else:
                    approvals.decide(a.id, ans, reason)
            time.sleep(0.02)


class EditorTests(TestCase):
    def setUp(self):
        # Agent tests below run unattended; the approval gate has its own tests that switch it back on.
        no_gate = self.settings(PSEDITOR_REQUIRE_APPROVAL=False)
        no_gate.enable()
        self.addCleanup(no_gate.disable)
        Agent.objects.all().delete()  # drop the OpenRouter agent seeded by migration 0003
        self.extra_shells = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ocr_cfg = self.settings(PSEDITOR_OCR_CONFIG=str(self.root / "ocr_settings.json"))
        ocr_cfg.enable()
        self.addCleanup(ocr_cfg.disable)
        (self.root / "hello.py").write_bytes(b"print('hi')\n")
        r = self.client.post("/api/workspaces", json.dumps({"path": str(self.root)}),
                             content_type="application/json")
        self.ws = r.json()["id"]

    def tearDown(self):
        import time
        shell.reset(self.ws)
        ptyterm.kill_session(self.ws)
        for s in self.extra_shells:
            s.kill()
        for attempt in range(20):  # Windows releases a killed process's cwd a moment later
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.25)

    def j(self, method, url, data=None):
        return getattr(self.client, method)(url, json.dumps(data or {}), content_type="application/json")

    # -- files -------------------------------------------------------------
    def test_index_and_bootstrap(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertRegex(html, r"app\.js\?v=\d+")   # cache-busting so browsers don't run a stale UI
        self.assertRegex(html, r"app\.css\?v=\d+")
        self.assertIn('id="attach"', html)
        self.assertIn('id="mic"', html)
        self.assertIn('id="mic-lang"', html)
        self.assertIn('id="image-view"', html)
        self.assertIn('id="btn-ocr"', html)
        self.assertIn('id="btn-help"', html)
        self.assertIn('id="image-ocr-settings"', html)
        boot = self.client.get("/api/bootstrap").json()
        self.assertEqual(boot["ocr_endpoint"], "https://api.opentyphoon.ai/v1/ocr")
        self.assertNotIn("ocr_api_key", boot)
        self.assertIn('id="think-level"', html)
        self.assertIn('id="think-show"', html)
        self.assertIn("no-cache", r["Cache-Control"])
        self.assertEqual(len(self.client.get("/api/bootstrap").json()["workspaces"]), 1)

    def test_file_crud(self):
        base = f"/api/ws/{self.ws}/file"
        tree = self.client.get(f"/api/ws/{self.ws}/tree").json()["entries"]
        self.assertEqual([e["name"] for e in tree], ["hello.py"])
        self.assertEqual(self.client.get(base, {"path": "hello.py"}).json()["content"], "print('hi')\n")
        self.assertEqual(self.j("put", base, {"path": "pkg/mod.py", "content": "x = 1\n"}).status_code, 200)
        self.assertEqual((self.root / "pkg" / "mod.py").read_bytes(), b"x = 1\n")
        self.assertEqual(self.j("patch", base, {"path": "pkg/mod.py", "new_path": "pkg/m2.py"}).status_code, 200)
        self.assertTrue((self.root / "pkg" / "m2.py").exists())
        self.assertEqual(self.j("delete", base, {"path": "pkg"}).status_code, 200)
        self.assertFalse((self.root / "pkg").exists())

    def test_open_with_asks_the_system_and_stays_in_the_workspace(self):
        import sys
        (self.root / "deck.pptx").write_bytes(b"PK\x03\x04")
        with mock.patch("editor.openwith.subprocess.Popen") as popen:
            ok = self.j("post", f"/api/ws/{self.ws}/open-with", {"path": "deck.pptx"})
        self.assertEqual(ok.status_code, 200, ok.content)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[-1], str(self.root / "deck.pptx"))
        if sys.platform == "win32":
            self.assertIn("SHOpenWithDialog", cmd[2])
        with mock.patch("editor.openwith.subprocess.Popen") as popen:
            blocked = self.j("post", f"/api/ws/{self.ws}/open-with", {"path": "../outside.pptx"})
        self.assertEqual(blocked.status_code, 400)
        popen.assert_not_called()
        self.assertFalse((self.root.parent / "outside.pptx").exists())

    def test_png_opens_as_an_image(self):
        png = _png_bytes()
        (self.root / "shot.png").write_bytes(png)
        r = self.client.get(f"/api/ws/{self.ws}/preview", {"path": "shot.png"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("image/png"))
        body = b"".join(r.streaming_content)
        r.close()
        self.assertEqual(body, png)
        # The text editor still refuses to treat PNG bytes as source.
        denied = self.client.get(f"/api/ws/{self.ws}/file", {"path": "shot.png"})
        self.assertEqual(denied.status_code, 400)
        self.assertIn("Binary", denied.json()["error"])

    def test_ocr_uses_configured_endpoint(self):
        png = _png_bytes()
        (self.root / "shot.png").write_bytes(png)
        (self.root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        base = f"/api/ws/{self.ws}/ocr"
        with self.settings(PSEDITOR_OCR_ENDPOINT="", PSEDITOR_OCR_API_KEY=""):
            missing = self.j("post", base, {"path": "shot.png"})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("not configured", missing.json()["error"])

        class Resp:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            text = '{"text": "Hello PNG"}'

            def json(self):
                return {"text": "Hello PNG"}

        with self.settings(PSEDITOR_OCR_ENDPOINT="http://ocr.local/read",
                           PSEDITOR_OCR_API_KEY="", PSEDITOR_OCR_MODEL=""):
            self.assertEqual(self.client.get("/api/bootstrap").json()["ocr_endpoint"], "http://ocr.local/read")
            with mock.patch("editor.ocr.requests.post", return_value=Resp()) as post:
                r = self.j("post", base, {"path": "shot.png"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["text"], "Hello PNG")
            self.assertEqual(post.call_args.args[0], "http://ocr.local/read")
            sent = post.call_args.kwargs["files"]["file"]
            self.assertEqual(sent[0], "shot.png")
            self.assertEqual(sent[2], "image/png")
            not_image = self.j("post", base, {"path": "hello.py"})
            self.assertEqual(not_image.status_code, 400)
            self.assertIn("image files", not_image.json()["error"])
            self.assertEqual(post.call_args.kwargs["headers"], {})
            self.assertIsNone(post.call_args.kwargs["data"])

        class TyphoonResp:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            text = ""

            def json(self):
                return {"results": [{"message": {"choices": [{"message": {"content": "สวัสดี"}}]}}]}

        with self.settings(PSEDITOR_OCR_ENDPOINT="https://api.opentyphoon.ai/v1/ocr",
                           PSEDITOR_OCR_API_KEY="sk-test", PSEDITOR_OCR_MODEL="typhoon-ocr"):
            with mock.patch("editor.ocr.requests.post", return_value=TyphoonResp()) as post:
                thai = self.j("post", base, {"path": "shot.png"})
            self.assertEqual(thai.status_code, 200)
            self.assertEqual(thai.json()["text"], "สวัสดี")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")
            body = post.call_args.kwargs["json"]
            self.assertEqual(post.call_args.args[0], "https://api.opentyphoon.ai/v1/chat/completions")
            self.assertEqual(body["model"], "typhoon-ocr")
            image = body["messages"][0]["content"][1]["image_url"]["url"]
            self.assertTrue(image.startswith("data:image/jpeg;base64,"))

        class TestResp:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": "OCR test"}}]}

        with self.settings(PSEDITOR_OCR_ENDPOINT="https://api.opentyphoon.ai/v1/ocr",
                           PSEDITOR_OCR_API_KEY="sk-test", PSEDITOR_OCR_MODEL="typhoon-ocr"):
            with mock.patch("editor.ocr.requests.post", return_value=TestResp()):
                checked = self.j("post", "/api/ocr-settings/test", {
                    "endpoint": "https://api.opentyphoon.ai/v1/ocr",
                    "model": "typhoon-ocr",
                    "api_key": "sk-test",
                }).json()
            self.assertTrue(checked["ok"])
            self.assertIn("OCR test", checked["message"])

    def test_ocr_settings_icon_saves_endpoint(self):
        with self.settings(PSEDITOR_OCR_API_KEY="sk-test"):
            current = self.client.get("/api/ocr-settings").json()
            self.assertEqual(current["endpoint"], "https://api.opentyphoon.ai/v1/ocr")
            self.assertTrue(current["has_key"])
            self.assertNotIn("api_key", current)
            saved = self.j("put", "/api/ocr-settings", {
                "endpoint": "https://ocr.example/v1/ocr",
                "model": "typhoon-ocr",
                "api_key": "",
            }).json()
            self.assertEqual(saved["endpoint"], "https://ocr.example/v1/ocr")
            self.assertTrue(saved["has_key"])
            again = self.client.get("/api/ocr-settings").json()
            self.assertEqual(again["endpoint"], "https://ocr.example/v1/ocr")
            self.assertNotIn("sk-", json.dumps(again))
            bad = self.j("put", "/api/ocr-settings", {"endpoint": "not a url", "model": "", "api_key": ""})
            self.assertEqual(bad.status_code, 400)

    def test_help_button_serves_both_manuals(self):
        en = self.client.get("/api/help").json()
        self.assertEqual(en["lang"], "en")
        self.assertIn('id="5-opening-folders-the-workspace"', en["html"])
        self.assertIn("Read text", en["html"])
        self.assertIn('href="#16-troubleshooting"', en["html"])
        self.assertNotIn("<script>", en["html"])
        thai = self.client.get("/api/help", {"lang": "th"}).json()
        self.assertEqual(thai["lang"], "th")
        self.assertIn("การแก้ไขไฟล์", thai["html"])
        self.assertIn("อ่านข้อความในรูป", thai["html"])
        other = self.client.get("/api/help", {"lang": "fr"}).json()
        self.assertEqual(other["lang"], "en")
        from .manual import render_md
        rendered = render_md("See <script>alert(1)</script> and [docs](https://example.com).")
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn('href="https://example.com"', rendered)

    def test_path_traversal_blocked(self):
        base = f"/api/ws/{self.ws}/file"
        self.assertEqual(self.client.get(base, {"path": "../secret.txt"}).status_code, 400)
        r = self.j("put", base, {"path": "../evil.txt", "content": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_search(self):
        r = self.j("post", f"/api/ws/{self.ws}/search", {"query": "print"})
        self.assertEqual(r.json()["results"][0]["path"], "hello.py")

    # -- terminal (persistent real shell) -------------------------------------
    def term(self, command=""):
        return self.j("post", f"/api/ws/{self.ws}/terminal", {"command": command}).json()

    def test_terminal_prompt_is_real_path_and_cd_is_unrestricted(self):
        r = self.term()  # no command: just the prompt
        self.assertEqual(r["prompt"], str(self.root.resolve()) + ">")
        self.assertNotIn(self.root.name + " >", r["prompt"])
        r = self.term("echo pseditor-ok")
        self.assertIn("pseditor-ok", r["output"])
        (self.root / "sub").mkdir()
        r = self.term("cd sub")
        self.assertEqual(r["cwd"], str((self.root / "sub").resolve()))
        self.assertTrue(r["prompt"].endswith("sub>"))
        r = self.term("cd ..")
        self.assertEqual(r["cwd"], str(self.root.resolve()))
        # not confined to the workspace: go to the parent and to the drive root
        parent = self.root.resolve().parent
        self.assertEqual(self.term("cd ..")["cwd"], str(parent))
        anchor = Path(parent.anchor)
        self.assertEqual(self.term(f'cd "{anchor}"')["cwd"], str(anchor))
        self.assertEqual(self.term(f'cd "{self.root}"')["cwd"], str(self.root.resolve()))

    def test_terminal_state_persists_and_reports_exit_codes(self):
        if shell.IS_WIN:
            self.term("$x = 41")
            self.assertIn("42", self.term("$x + 1")["output"])
            self.term("$env:PSE_TEST = 'abc'")
            self.assertIn("abc", self.term("$env:PSE_TEST")["output"])
            (self.root / "sub").mkdir()
            self.assertIn("sub", self.term("ls")["output"])
            self.assertEqual(self.term('python -c "import sys; sys.exit(3)"')["exit_code"], 3)
            bad = self.term("nosuchcommand-xyz")
            self.assertNotEqual(bad["exit_code"], 0)
            self.assertIn("nosuchcommand-xyz", bad["output"])
            self.assertNotIn("FromBase64String", bad["output"])  # no wrapper internals leaking
        else:
            self.term("x=41")
            self.assertIn("42", self.term("echo $((x+1))")["output"])
        self.assertEqual(self.term("echo hi")["exit_code"], 0)

    def test_terminal_venv_activation_shows_prefix(self):
        import venv
        venv.create(self.root / "venv", with_pip=False)
        scripts = "Scripts" if shell.IS_WIN else "bin"
        self.assertTrue((self.root / "venv" / scripts).is_dir())
        cmd = "venv/scripts/activate" if shell.IS_WIN else "source venv/bin/activate"
        r = self.term(cmd)
        self.assertTrue(r["prompt"].startswith("(venv) "), r)
        self.assertEqual(r["venv"], "(venv)")
        self.assertTrue(self.term("echo hi")["prompt"].startswith("(venv) "))
        r = self.term("deactivate")
        self.assertFalse(r["prompt"].startswith("("), r)

    @unittest.skipUnless(shell.IS_WIN, "PowerShell-specific activation spellings")
    def test_terminal_venv_activation_spellings(self):
        import venv
        venv.create(self.root / "venv", with_pip=False)
        for cmd in [
            r"venv\Scripts\activate", "venv/scripts/activate", r".\venv\Scripts\activate",
            r"venv\Scripts\activate.bat",            # cmd habit: used to silently do nothing
            r"call venv\Scripts\activate.bat", r"call venv\Scripts\activate",
            "source venv/Scripts/activate", r"venv\Scripts\Activate.ps1", r"& venv\Scripts\Activate.ps1",
            f'"{self.root / "venv" / "Scripts" / "activate.bat"}"',
            r"cd venv\Scripts; activate",             # bare `activate` after cd on one line
        ]:
            shell.reset(self.ws)
            r = self.term(cmd)
            self.assertTrue(r["prompt"].startswith("(venv) "), (cmd, r))
        shell.reset(self.ws)
        self.term(r"cd venv\Scripts")
        self.assertTrue(self.term("activate")["prompt"].startswith("(venv) "))
        self.assertFalse(self.term(r"venv\Scripts\deactivate.bat")["prompt"].startswith("("))
        # typed on separate lines, `.\activate.bat` is mapped too (cwd is known by then)
        shell.reset(self.ws)
        self.term(r"cd venv\Scripts")
        self.assertTrue(self.term(r".\activate.bat")["prompt"].startswith("(venv) "))
        # in a folder without Activate.ps1, `activate` is a plain error, not a crash
        shell.reset(self.ws)
        r = self.term("activate")
        self.assertNotEqual(r["exit_code"], 0)
        self.assertFalse(r["prompt"].startswith("("))

    def test_terminal_interrupt_and_timeout(self):
        # Exercised at the session level (the HTTP view is a thin wrapper) to avoid sharing the
        # in-memory test database with a worker thread.
        import threading
        import time
        s = shell.ShellSession(self.root)
        self.extra_shells.append(s)
        sleeper = "Start-Sleep 30" if shell.IS_WIN else "sleep 30"
        result = {}
        t = threading.Thread(target=lambda: result.update(s.run(sleeper)))
        t0 = time.monotonic()
        t.start()
        time.sleep(1.5)
        s.interrupt()
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 25)
        self.assertIn("^C", result["output"])
        self.assertIn("hi", s.run("echo hi")["output"])  # a fresh shell took over
        slow = s.run(sleeper, timeout=2)
        self.assertEqual(slow["exit_code"], 124)
        self.assertIn("timed out", slow["output"])
        self.assertIn("hi", s.run("echo hi")["output"])
    # -- real terminal (PTY) ---------------------------------------------------
    def pty(self, **kw):
        s = ptyterm.PtySession(self.root, **kw)
        self.extra_shells.append(s)
        return s

    def wait_for(self, s, text, timeout=20, since=0):
        import time
        deadline = time.monotonic() + timeout
        out = ""
        while time.monotonic() < deadline:
            out = s.read(since, 0.3)["data"]
            if text in out:
                return out
        self.fail(f"{text!r} not seen within {timeout}s; got: {out[-600:]!r}")

    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_runs_commands_and_prompt_is_the_real_folder(self):
        s = self.pty()
        self.wait_for(s, str(self.root.resolve()) + "> ")
        s.write("echo ('pty-' + 'ok')\r")
        self.wait_for(s, "pty-ok")
        (self.root / "sub").mkdir()
        s.write("cd sub; (Get-Location).Path + '!'\r")
        self.wait_for(s, str((self.root / "sub").resolve()) + "!")
        s.write("cd ..\\..; (Get-Location).Path + '?'\r")  # not confined to the workspace
        self.wait_for(s, str(self.root.resolve().parent) + "?")

    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_is_a_real_tty_with_interactive_input(self):
        s = self.pty()
        self.wait_for(s, "> ")
        s.write("python -c \"import sys; print('tty=' + str(sys.stdin.isatty()))\"\r")
        self.wait_for(s, "tty=True")
        s.write("python -c \"n = input('name? '); print('hello-' + n)\"\r")
        self.wait_for(s, "name? ")
        s.write("bob\r")
        self.wait_for(s, "hello-bob")
        s.write("python -c \"import getpass; print('pw-' + getpass.getpass('secret: '))\"\r")
        self.wait_for(s, "secret: ")
        s.write("hunter2\r")
        self.wait_for(s, "pw-hunter2")

    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_ctrl_c_interrupts_running_command(self):
        s = self.pty()
        self.wait_for(s, "> ")
        s.write("Start-Sleep 60\r")
        import time
        time.sleep(1.5)
        s.write("\x03")
        time.sleep(1.0)  # PowerShell redraws after ^C; a human would not type within the same millisecond
        s.write("echo ('af' + 'ter')\r")
        self.wait_for(s, "after", timeout=8)  # would take 60s if the sleep were not interrupted

    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_django_createsuperuser_is_interactive(self):
        import sys
        db = self.root / "t.sqlite3"
        (self.root / "tmp_settings.py").write_text(
            "from pseditor.settings import *\n"
            f"DATABASES = {{'default': {{'ENGINE': 'django.db.backends.sqlite3', 'NAME': r'{db}'}}}}\n")
        proj = Path(__file__).resolve().parent.parent
        s = self.pty()
        self.wait_for(s, "> ")
        s.write(f"$env:PYTHONPATH = '{self.root};{proj}'; $env:DJANGO_SETTINGS_MODULE = 'tmp_settings'\r")
        s.write(f'& "{sys.executable}" "{proj / "manage.py"}" migrate --noinput | Select-Object -Last 1\r')
        self.wait_for(s, "sessions.0001_initial", timeout=60)
        s.write(f'& "{sys.executable}" "{proj / "manage.py"}" createsuperuser\r')
        self.wait_for(s, "Username", timeout=30)
        s.write("boss\r")
        self.wait_for(s, "Email address")
        s.write("boss@example.com\r")
        self.wait_for(s, "Password: ")
        s.write("Tr1cky-Passw0rd-xyz\r")
        self.wait_for(s, "Password (again): ")
        s.write("Tr1cky-Passw0rd-xyz\r")
        out = self.wait_for(s, "Superuser created successfully", timeout=30)
        self.assertNotIn("not running in a TTY", out)

    @unittest.skipUnless(ptyterm.available() and shell.IS_WIN, "PowerShell venv hook")
    def test_pty_venv_activation_spellings_show_prefix(self):
        import venv
        venv.create(self.root / "venv", with_pip=False)
        for cmd in [r"venv\Scripts\activate", "venv/scripts/activate", r".\venv\Scripts\activate",
                    r"venv\Scripts\activate.bat", r"call venv\Scripts\activate.bat",
                    "source venv/Scripts/activate", r"venv\Scripts\Activate.ps1",
                    r"cd venv\Scripts; activate"]:
            s = self.pty()
            self.wait_for(s, "> ")
            s.write(cmd + "\r")
            self.wait_for(s, "(venv)", timeout=15)   # only the prompt prints this (never the typed text)
            s.write("deactivate\r")
        s = self.pty()
        self.wait_for(s, "> ")
        s.write(r"venv\Scripts\activate" + "\r")
        self.wait_for(s, "(venv)")
        s.write(r"venv\Scripts\deactivate.bat" + "\r")
        s.write("echo ('va' + 'r=' + $env:VIRTUAL_ENV + '.')\r")
        self.wait_for(s, "var=.", timeout=10)   # VIRTUAL_ENV is empty again
    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_output_offsets_resize_and_scrollback(self):
        s = self.pty(cols=100, rows=24)
        self.wait_for(s, "> ")
        full = s.read(0)
        self.assertFalse(full["reset"])
        self.assertEqual(s.read(full["end"], 0.2)["data"], "")      # nothing new
        s.write("echo ('of' + 'fset')\r")
        self.wait_for(s, "offset", since=full["end"])
        s.resize(120, 30)
        s.write("$Host.UI.RawUI.WindowSize.Width.ToString() + 'w'\r")
        self.wait_for(s, "120w")
        s._push("x" * (ptyterm.MAX_BUFFER + 10))                    # force a trim
        self.assertTrue(s.read(0)["reset"])
        self.assertLessEqual(len(s.buf), ptyterm.MAX_BUFFER)

    @unittest.skipUnless(ptyterm.available(), "pywinpty / pty not available")
    def test_pty_http_api(self):
        url = f"/api/ws/{self.ws}/pty"
        r = self.j("post", f"{url}/open", {"cols": 90, "rows": 20})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["sid"]
        self.assertEqual(self.j("post", f"{url}/open", {}).json()["sid"], sid)   # reattach
        end = 0
        for _ in range(60):
            d = self.client.get(f"{url}/read", {"sid": sid, "since": end, "wait": 0.5}).json()
            end = d["end"]
            if "> " in d["data"]:
                break
        self.assertIn(str(self.root.resolve()), d["data"])
        self.assertEqual(self.j("post", f"{url}/input", {"sid": sid, "data": "echo ('ht' + 'tp')\r"}).status_code, 200)
        got = ""
        for _ in range(60):
            d = self.client.get(f"{url}/read", {"sid": sid, "since": end, "wait": 0.5}).json()
            end, got = d["end"], got + d["data"]
            if "http" in got:
                break
        self.assertIn("http", got)
        self.assertEqual(self.j("post", f"{url}/resize", {"sid": sid, "cols": 100, "rows": 30}).status_code, 200)
        self.assertEqual(self.client.get(f"{url}/read", {"sid": "stale", "since": 0}).status_code, 409)
        self.assertEqual(self.j("post", f"{url}/input", {"sid": "stale", "data": "x"}).status_code, 409)
        new = self.j("post", f"{url}/open", {"restart": True}).json()["sid"]
        self.assertNotEqual(new, sid)
        self.assertEqual(self.client.get(f"{url}/read", {"sid": sid, "since": 0}).status_code, 409)
        self.assertTrue(self.client.get("/api/bootstrap").json()["pty"])

    @unittest.skipUnless(ptyterm.available() and shell.IS_WIN, "Windows job object")
    def test_pty_shell_dies_with_an_abruptly_killed_server(self):
        import subprocess
        import sys
        import time
        proj = Path(__file__).resolve().parent.parent
        child = (
            "import os, sys, time\n"
            f"sys.path.insert(0, r'{proj}')\n"
            "os.environ['DJANGO_SETTINGS_MODULE'] = 'pseditor.settings'\n"
            "import django; django.setup()\n"
            "from editor import ptyterm\n"
            f"s = ptyterm.PtySession(r'{self.root}')\n"
            "print(s.backend.p.pid, flush=True)\n"
            "time.sleep(60)\n"
        )

        def alive(pid):
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            return str(pid) in out

        server = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
        shell_pid = int(server.stdout.readline().strip())
        try:
            time.sleep(1.5)
            self.assertTrue(alive(shell_pid))
            subprocess.run(["taskkill", "/F", "/PID", str(server.pid)], capture_output=True)  # no atexit runs
            for _ in range(20):
                time.sleep(0.5)
                if not alive(shell_pid):
                    break
            self.assertFalse(alive(shell_pid), "the shell was orphaned")
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(shell_pid)], capture_output=True)
            server.stdout.close()

    def test_pty_respects_disabled_setting(self):
        with self.settings(PSEDITOR_ALLOW_TERMINAL=False):
            self.assertEqual(self.j("post", f"/api/ws/{self.ws}/pty/open", {}).status_code, 400)
            self.assertEqual(self.client.get(f"/api/ws/{self.ws}/pty/read", {"sid": "x"}).status_code, 400)

    def test_terminal_disabled_setting(self):
        with self.settings(PSEDITOR_ALLOW_TERMINAL=False):
            r = self.j("post", f"/api/ws/{self.ws}/terminal", {"command": "echo hi"})
            self.assertEqual(r.status_code, 400)

    def test_agent_run_command_stays_confined_to_workspace(self):
        from .fsops import WorkspaceFS
        from .tools import ToolContext
        ctx = ToolContext(WorkspaceFS(self.root))
        ok, _ = ctx.execute("run_command", {"command": "echo hi", "cwd": ".."})
        self.assertFalse(ok)

    # -- agents ------------------------------------------------------------
    def _session(self):
        return self.j("post", f"/api/ws/{self.ws}/sessions").json()["id"]

    def test_agent_validation_and_default(self):
        r = self.j("post", "/api/agents", {"name": "x", "provider": "openai"})
        self.assertEqual(r.status_code, 400)  # model required
        r = self.j("post", "/api/agents", {"name": "a", "provider": "openai", "model": "m", "api_key": "k"})
        self.assertTrue(r.json()["is_default"])
        self.assertNotIn("api_key", r.json())
        self.assertTrue(r.json()["has_key"])

    def test_openrouter_is_seeded_as_default_agent(self):
        from importlib import import_module
        from django.apps import apps as django_apps
        seed = import_module("editor.migrations.0003_default_openrouter_agent").add_openrouter_agent
        seed(django_apps, None)
        agent = Agent.objects.get()
        self.assertEqual((agent.name, agent.base_url, agent.is_default),
                         ("OpenRouter", "https://openrouter.ai/api/v1", True))
        seed(django_apps, None)  # existing agents are left alone
        self.assertEqual(Agent.objects.count(), 1)

        sid = self._session()
        r = self.j("post", f"/api/sessions/{sid}/chat", {"message": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("OpenRouter API key", r.json()["error"])

    def test_openai_agent_tool_loop_and_revert(self):
        self.j("post", "/api/agents", {"name": "gpt", "provider": "openai", "model": "m",
                                       "base_url": "http://fake/v1", "api_key": "k"})
        sid = self._session()
        replies = [
            {"choices": [{"message": {"content": "Creating file", "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "app.py", "content": "print(1)\n"})}},
                {"id": "c2", "type": "function", "function": {
                    "name": "edit_file", "arguments": json.dumps(
                        {"path": "hello.py", "old_string": "hi", "new_string": "hello"})}},
            ]}}]},
            {"choices": [{"message": {"content": "Done!"}}]},
        ]
        with mock.patch("editor.agents.requests.post", side_effect=[FakeResp(r) for r in replies]) as post:
            resp = self.j("post", f"/api/sessions/{sid}/chat", {"message": "make an app"})
            events = stream_events(resp)
        types = [e["type"] for e in events]
        self.assertIn("tool_call", types)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(sorted(events[-1]["changed"]), ["app.py", "hello.py"])
        self.assertEqual((self.root / "app.py").read_bytes(), b"print(1)\n")
        self.assertEqual((self.root / "hello.py").read_bytes(), b"print('hello')\n")
        # second request must contain tool result messages in OpenAI format
        sent = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual([m["role"] for m in sent][-2:], ["tool", "tool"])
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer k")

        detail = self.client.get(f"/api/sessions/{sid}").json()
        user_msg = detail["messages"][0]
        self.assertEqual(sorted(user_msg["changed"]), ["app.py", "hello.py"])
        r = self.j("post", f"/api/messages/{user_msg['id']}/revert")
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.root / "app.py").exists())
        self.assertEqual((self.root / "hello.py").read_bytes(), b"print('hi')\n")
        self.assertEqual(self.j("post", f"/api/messages/{user_msg['id']}/revert").status_code, 400)

    def test_anthropic_agent(self):
        self.j("post", "/api/agents", {"name": "claude", "provider": "anthropic", "api_key": "k"})
        sid = self._session()
        replies = [
            {"content": [{"type": "text", "text": "ok"},
                         {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "hello.py"}}]},
            {"content": [{"type": "text", "text": "It prints hi."}]},
        ]
        with mock.patch("editor.agents.requests.post", side_effect=[FakeResp(r) for r in replies]) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "what does it do"}))
        self.assertEqual(post.call_args_list[0].args[0], "https://api.anthropic.com/v1/messages")
        second = post.call_args_list[1].kwargs["json"]["messages"]
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual([e["content"] for e in events if e["type"] == "text"], ["ok", "It prints hi."])

    def test_webhook_agent(self):
        self.j("post", "/api/agents", {"name": "hook", "provider": "webhook", "base_url": "http://fake/agent"})
        sid = self._session()
        reply = {"reply": "Created it", "actions": [
            {"type": "write_file", "path": "new/dir/x.txt", "content": "data"},
            {"type": "run_command", "command": "echo hi"},
        ]}
        with mock.patch("editor.agents.requests.post", return_value=FakeResp(reply)) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {
                "message": "do it", "active_file": {"path": "hello.py", "selection": "print"}}))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["instruction"], "do it")
        self.assertEqual(payload["active_file"]["content"], "print('hi')\n")
        self.assertEqual((self.root / "new" / "dir" / "x.txt").read_bytes(), b"data")
        self.assertEqual(events[-1]["changed"], ["new/dir/x.txt"])

    def test_cli_agent_detects_changes(self):
        cmd = 'python -c "import sys,pathlib; pathlib.Path(\'out.txt\').write_text(sys.stdin.read()[:5]); print(\'done\')"'
        self.j("post", "/api/agents", {"name": "cli", "provider": "cli", "command": cmd})
        sid = self._session()
        events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "abcdefgh"}))
        self.assertEqual(events[-1]["changed"], ["out.txt"])
        self.assertTrue((self.root / "out.txt").read_text().strip())
        self.assertIn("done", [e["content"] for e in events if e["type"] == "text"][0])

    def test_prepare_attachments_reads_image_pdf_excel_and_word(self):
        from .attachments import prepare_attachments
        from .fsops import FsError

        png = _png_bytes()
        prepared = prepare_attachments([
            {"name": "note.pdf", "mime": "application/pdf", "data": _b64(_pdf_bytes("Hello PDF"))},
            {"name": "sheet.xlsx", "mime": "", "data": _b64(_xlsx_bytes())},
            {"name": "old.xls", "data": _b64(_xls_bytes())},
            {"name": "brief.docx", "data": _b64(_docx_bytes())},
        ])
        self.assertIn("Hello PDF", prepared.text)
        self.assertIn("Apple", prepared.text)
        self.assertIn("Pen", prepared.text)
        self.assertIn("Hello Word", prepared.text)
        self.assertIn("Qty", prepared.text)
        self.assertEqual(prepared.images, [])

        image = prepare_attachments([
            {"name": "shot.png", "mime": "image/png", "data": _b64(png)},
        ])
        self.assertEqual(len(image.images), 1)
        self.assertTrue(image.images[0]["data"])
        self.assertTrue(image.images[0]["mime"].startswith("image/"))
        self.assertEqual(image.meta[0]["kind"], "image")
        self.assertNotIn("data", image.meta[0])

        doc = prepare_attachments([{
            "name": "legacy.doc",
            "data": _b64(_doc_bytes("Budget approval for the new warehouse")),
        }])
        self.assertIn("Budget approval", doc.text)

        with self.assertRaises(FsError) as raised:
            prepare_attachments([{"name": "virus.exe", "data": _b64(b"MZ")}])
        self.assertIn("not a supported attachment", str(raised.exception))
        with mock.patch("editor.attachments.MAX_FILE_BYTES", 8):
            with self.assertRaises(FsError) as raised:
                prepare_attachments([{"name": "big.png", "data": _b64(b"0123456789")}])
        self.assertIn("larger than", str(raised.exception))

    def test_chat_sends_attachments_to_the_model(self):
        from . import agents
        self.j("post", "/api/agents", {"name": "gpt", "provider": "openai", "model": "m",
                                       "base_url": "http://fake/v1", "api_key": "k"})
        sid = self._session()
        attachments = [
            {"name": "note.pdf", "mime": "application/pdf", "data": _b64(_pdf_bytes("Hello PDF"))},
            {"name": "sheet.xlsx", "data": _b64(_xlsx_bytes())},
            {"name": "brief.docx", "data": _b64(_docx_bytes())},
            {"name": "shot.png", "mime": "image/png", "data": _b64(_png_bytes())},
        ]
        replies = [
            {"choices": [{"message": {"content": "I can see the files."}}]},
            {"choices": [{"message": {"content": "Still red."}}]},
        ]
        with mock.patch("editor.ocr.text_for_chat_images", return_value=""), \
             mock.patch("editor.agents.requests.post", side_effect=[FakeResp(r) for r in replies]) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                          {"message": "summarize these", "attachments": attachments}))
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "what color?"}))
        self.assertEqual(events[-1]["type"], "done")
        user = post.call_args_list[0].kwargs["json"]["messages"][1]
        self.assertIsInstance(user["content"], list)
        text = user["content"][0]["text"]
        self.assertIn("Hello PDF", text)
        self.assertIn("Apple", text)
        self.assertIn("Hello Word", text)
        self.assertEqual(user["content"][1]["type"], "image_url")
        self.assertTrue(user["content"][1]["image_url"]["url"].startswith("data:image/"))
        # a later turn still shows the model the image
        again = post.call_args_list[1].kwargs["json"]["messages"][1]
        self.assertEqual(again["content"][1]["type"], "image_url")

        blocks = agents._anthropic_user_blocks({
            "role": "user", "content": "see this",
            "images": [{"name": "shot.png", "mime": "image/png", "data": "abcd"}],
        })
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        self.assertEqual(blocks[0]["source"]["data"], "abcd")
        self.assertEqual(blocks[1]["text"], "see this")

        detail = self.client.get(f"/api/sessions/{sid}").json()
        shown = detail["messages"][0]
        self.assertEqual(shown["text"], "summarize these")
        self.assertEqual([a["name"] for a in shown["attachments"]],
                         ["note.pdf", "sheet.xlsx", "brief.docx", "shot.png"])
        self.assertTrue(all("data" not in a for a in shown["attachments"]))
        self.assertEqual(detail["session"]["title"], "summarize these")

    def test_chat_rejects_unsupported_attachment(self):
        sid = self._session()
        r = self.j("post", f"/api/sessions/{sid}/chat", {
            "message": "hi",
            "attachments": [{"name": "virus.exe", "data": _b64(b"MZ")}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a supported attachment", r.json()["error"])

    def test_cli_agent_receives_attachment_text(self):
        cmd = 'python -c "import sys,pathlib; pathlib.Path(\'got.txt\').write_text(sys.stdin.read(), encoding=\'utf-8\')"'
        self.j("post", "/api/agents", {"name": "cli", "provider": "cli", "command": cmd})
        sid = self._session()
        events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {
            "message": "read this",
            "attachments": [{"name": "note.pdf", "mime": "application/pdf", "data": _b64(_pdf_bytes("Hello PDF"))}],
        }))
        self.assertEqual(events[-1]["type"], "done")
        self.assertIn("Hello PDF", (self.root / "got.txt").read_text(encoding="utf-8"))

    def test_webhook_receives_attachment_text_and_image(self):
        self.j("post", "/api/agents", {"name": "hook", "provider": "webhook", "base_url": "http://fake/agent"})
        sid = self._session()
        with mock.patch("editor.ocr.text_for_chat_images", return_value=""), \
             mock.patch("editor.agents.requests.post", return_value=FakeResp({"reply": "ok", "actions": []})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {
                "message": "look",
                "attachments": [{"name": "shot.png", "mime": "image/png", "data": _b64(_png_bytes())}],
            }))
        payload = post.call_args.kwargs["json"]
        self.assertIn("shot.png", payload["instruction"])
        self.assertEqual(payload["attachments"][0]["kind"], "image")
        self.assertTrue(payload["images"][0]["data"])
        self.assertNotIn("data", payload["attachments"][0])

    def test_text_only_model_reads_the_image_through_ocr(self):
        from . import agents
        agents._MODELS_WITHOUT_VISION.discard("text-only-model")
        self.j("post", "/api/agents", {"name": "gpt", "provider": "openai", "model": "text-only-model",
                                       "base_url": "http://fake/v1", "api_key": "k"})
        sid = self._session()
        rejected = FakeResp(
            {"error": {"message": "No endpoints found that support image input"}}, status=404)
        ok = FakeResp({"choices": [{"message": {"content": "the slip says hello"}}]})
        with mock.patch("editor.ocr.text_for_chat_images", return_value="### shot.png\nสวัสดี"), \
             mock.patch("editor.agents.requests.post", side_effect=[rejected, ok]) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {
                "message": "read this",
                "attachments": [{"name": "shot.png", "mime": "image/png", "data": _b64(_png_bytes())}],
            }))
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(any(e.get("type") == "text" and "the slip says hello" in e.get("content", "") for e in events))
        first = post.call_args_list[0].kwargs["json"]["messages"][1]["content"]
        second = post.call_args_list[1].kwargs["json"]["messages"][1]["content"]
        self.assertIsInstance(first, list)
        self.assertEqual(first[1]["type"], "image_url")
        self.assertIsInstance(second, str)
        self.assertIn("สวัสดี", second)
        self.assertNotIn("image_url", second)
        agents._MODELS_WITHOUT_VISION.discard("text-only-model")

    def test_read_file_uses_ocr_for_a_png(self):
        from .tools import ToolContext
        from .views import ws_fs
        (self.root / "shot.png").write_bytes(_png_bytes())
        _, fs = ws_fs(self.ws)
        with mock.patch("editor.ocr.recognize_bytes", return_value="ใบเสร็จ"):
            ok, text = ToolContext(fs).execute("read_file", {"path": "shot.png"})
        self.assertTrue(ok)
        self.assertEqual(text, "ใบเสร็จ")

    # -- human in the loop -------------------------------------------------
    def _tool_replies(self, *calls):
        """OpenAI-style replies: one assistant turn with these (name, args) tool calls, then a final answer."""
        tcs = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
               for i, (n, a) in enumerate(calls)]
        return [FakeResp({"choices": [{"message": {"content": "working", "tool_calls": tcs}}]}),
                FakeResp({"choices": [{"message": {"content": "finished"}}]})]

    def _gpt(self):
        self.j("post", "/api/agents", {"name": "gpt", "provider": "openai", "model": "m",
                                       "base_url": "http://fake/v1", "api_key": "k"})
        return self._session()

    def _chat(self, sid, replies, human=None, text="go"):
        if human:
            human.start()
        try:
            with mock.patch("editor.agents.requests.post", side_effect=replies) as post:
                events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": text}))
        finally:
            if human:
                human.halt = True
                human.join(2)
        return events, post

    def test_approval_nothing_runs_until_the_user_approves(self):
        sid = self._gpt()
        target = self.root / "app.py"
        existed_when_asked = []
        human = Human(["allow", "allow"], check=lambda a: existed_when_asked.append(target.exists()))
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            events, _ = self._chat(sid, self._tool_replies(
                ("write_file", {"path": "app.py", "content": "print(1)\n"}),
                ("edit_file", {"path": "hello.py", "old_string": "hi", "new_string": "yo"})), human)
        self.assertEqual(existed_when_asked, [False, True])   # 1st: not yet written; 2nd: after 1st was approved
        self.assertEqual([n for n, _ in human.seen], ["write_file", "edit_file"])
        types = [e["type"] for e in events]
        # each approval is requested BEFORE the result of that very action: approval, result, approval, result
        self.assertEqual([t for t in types if t in ("approval", "tool_result")],
                         ["approval", "tool_result", "approval", "tool_result"])
        self.assertEqual([e["decision"] for e in events if e["type"] == "approval_result"], ["allow", "allow"])
        self.assertEqual(target.read_bytes(), b"print(1)\n")
        self.assertEqual((self.root / "hello.py").read_bytes(), b"print('yo')\n")
        self.assertEqual(events[-1]["type"], "done")

    def test_approval_denied_action_is_not_run_and_the_agent_is_told_why(self):
        sid = self._gpt()
        human = Human([("deny", "use hello.py instead"), "allow"])
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            events, post = self._chat(sid, self._tool_replies(
                ("write_file", {"path": "bad.py", "content": "x"}),
                ("run_command", {"command": "echo ran > ran.txt"})), human)
        self.assertFalse((self.root / "bad.py").exists())
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertFalse(results[0]["ok"])
        self.assertIn("denied", results[0]["result"])
        self.assertIn("use hello.py instead", results[0]["result"])
        self.assertTrue(results[1]["ok"])                       # the second action was approved and ran
        self.assertTrue((self.root / "ran.txt").exists())
        sent = post.call_args_list[1].kwargs["json"]["messages"]   # the model sees the denial
        self.assertIn("use hello.py instead", sent[-2]["content"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["changed"], ["ran.txt"])

    def test_approval_reads_are_free_unless_configured(self):
        sid = self._gpt()
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            events, _ = self._chat(sid, self._tool_replies(("read_file", {"path": "hello.py"}),
                                                           ("list_files", {})), Human([]))
            self.assertNotIn("approval", [e["type"] for e in events])
            self.assertTrue(all(e["ok"] for e in events if e["type"] == "tool_result"))
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True, PSEDITOR_APPROVE_READS=True):
            human = Human(["deny"])
            events, _ = self._chat(sid, self._tool_replies(("read_file", {"path": "hello.py"})), human)
            self.assertEqual([n for n, _ in human.seen], ["read_file"])
            self.assertFalse([e for e in events if e["type"] == "tool_result"][0]["ok"])

    def test_approval_timeout_runs_nothing(self):
        sid = self._gpt()
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True, PSEDITOR_APPROVAL_TIMEOUT=0.3):
            events, _ = self._chat(sid, self._tool_replies(("write_file", {"path": "late.py", "content": "x"})))
        self.assertFalse((self.root / "late.py").exists())
        self.assertEqual(events[-1]["type"], "done")
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertTrue(errors and "Timed out" in errors[0])
        self.assertEqual([e["decision"] for e in events if e["type"] == "approval_result"], ["timeout"])
        self.assertEqual(approvals._pending, {})

    def test_approval_stop_releases_the_waiting_action(self):
        sid = self._gpt()
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            events, _ = self._chat(sid, self._tool_replies(("run_command", {"command": "echo hi > x.txt"})),
                                   Human(["stop"]))
        self.assertFalse((self.root / "x.txt").exists())
        self.assertEqual([e["decision"] for e in events if e["type"] == "approval_result"], ["cancelled"])
        self.assertIn("Stopped", [e["message"] for e in events if e["type"] == "error"][0])
        self.assertEqual(approvals._pending, {})

    def test_approval_endpoints(self):
        sid = self._session()
        a = approvals.create(sid, "write_file", {"path": "p"})
        self.assertEqual(self.j("post", f"/api/approvals/{a.id}", {"decision": "maybe"}).status_code, 400)
        self.assertEqual(self.j("post", "/api/approvals/nope", {"decision": "allow"}).status_code, 409)
        r = self.j("post", f"/api/approvals/{a.id}", {"decision": "deny", "reason": "no thanks"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((a.decision, a.reason), ("deny", "no thanks"))
        self.assertEqual(self.j("post", f"/api/approvals/{a.id}", {"decision": "allow"}).status_code, 409)  # once
        approvals.discard(a.id)   # nobody is waiting on it in this test
        b = approvals.create(sid, "run_command", {"command": "x"})
        self.assertEqual(self.j("post", f"/api/sessions/{sid}/stop").json(), {"released": 1})
        self.assertEqual(approvals.wait(b, 0.1), approvals.CANCELLED)
        self.assertEqual(self.j("post", "/api/sessions/99999/stop").status_code, 404)

    def test_approval_webhook_and_cli_agents_are_gated(self):
        self.j("post", "/api/agents", {"name": "hook", "provider": "webhook", "base_url": "http://fake/agent"})
        sid = self._session()
        reply = {"reply": "done", "actions": [{"type": "write_file", "path": "w.txt", "content": "1"},
                                              {"type": "write_file", "path": "v.txt", "content": "2"}]}
        human = Human(["deny", "allow"])
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            human.start()
            try:
                with mock.patch("editor.agents.requests.post", return_value=FakeResp(reply)):
                    events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "x"}))
            finally:
                human.halt = True
                human.join(2)
        self.assertFalse((self.root / "w.txt").exists())
        self.assertEqual((self.root / "v.txt").read_text(), "2")
        self.assertEqual(events[-1]["changed"], ["v.txt"])

        cmd = 'python -c "import pathlib; pathlib.Path(\'cli_out.txt\').write_text(\'x\')"'
        self.j("post", "/api/agents", {"name": "cli", "provider": "cli", "command": cmd, "is_default": True})
        agent_id = Agent.objects.get(name="cli").pk
        sid = self._session()
        human = Human([("deny", "not now")])
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            human.start()
            try:
                events = stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                              {"message": "go", "agent_id": agent_id}))
            finally:
                human.halt = True
                human.join(2)
        self.assertEqual([n for n, _ in human.seen], ["cli_agent"])
        self.assertFalse((self.root / "cli_out.txt").exists())
        self.assertIn("not now", [e["content"] for e in events if e["type"] == "text"][0])
        self.assertEqual(events[-1]["changed"], [])

    def test_approval_is_on_by_default(self):
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            for name in ("write_file", "edit_file", "delete_file", "generate_image", "run_command", "cli_agent", "anything_else"):
                self.assertTrue(approvals.required(name), name)
            for name in ("read_file", "list_files", "search", "workspace_info",
                         "list_datasets", "profile_data", "query_data", "sql_query", "extract_document"):
                self.assertFalse(approvals.required(name), name)
        # the shipped project setting must stay on
        from pseditor import settings as project_settings
        self.assertIs(project_settings.PSEDITOR_REQUIRE_APPROVAL, True)

    def test_openrouter_key_never_sent_to_openai(self):
        # agent created with the OpenAI URL but an OpenRouter key -> corrected on save
        r = self.j("post", "/api/agents", {"name": "or", "provider": "openai", "model": "m",
                                           "base_url": "https://api.openai.com/v1", "api_key": "sk-or-v1-abc"})
        self.assertEqual(r.json()["base_url"], "https://openrouter.ai/api/v1")
        # an already-saved wrong agent is also fixed at request time
        Agent.objects.filter(pk=r.json()["id"]).update(base_url="")
        with mock.patch("editor.agents.requests.get", return_value=FakeResp({"data": []})) as get:
            self.j("post", "/api/models", {"agent_id": r.json()["id"]})
        self.assertEqual(get.call_args.args[0], "https://openrouter.ai/api/v1/models")
        sid = self._session()
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"choices": [{"message": {"content": "hi"}}]})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "a"}))
        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")

    def test_thinking_level_is_sent_and_can_be_shown_later(self):
        self.j("post", "/api/agents", {"name": "or", "provider": "openai", "model": "m",
                                       "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-abc"})
        sid = self._session()
        details = [{"type": "reasoning.text", "text": "check the file first", "format": "x"}]
        replies = [
            FakeResp({"choices": [{"message": {
                "content": "Reading it.",
                "reasoning": "check the file first",
                "reasoning_details": details,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "hello.py"})}}]}}]}),
            FakeResp({"choices": [{"message": {"content": "It prints hi.", "reasoning": "now I know"}}]}),
        ]
        with mock.patch("editor.agents.requests.post", side_effect=replies) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                          {"message": "what", "thinking": "high"}))
        first, second = [c.kwargs["json"] for c in post.call_args_list]
        self.assertEqual(first["reasoning"], {"effort": "high"})
        self.assertEqual(second["reasoning"], {"effort": "high"})
        self.assertEqual(second["messages"][2]["reasoning_details"], details)
        self.assertEqual([e["content"] for e in events if e["type"] == "thinking"],
                         ["check the file first", "now I know"])
        saved = self.client.get(f"/api/sessions/{sid}").json()["messages"]
        thoughts = [m["thinking"] for m in saved if m["role"] == "assistant" and m.get("thinking")]
        self.assertEqual(thoughts, ["check the file first", "now I know"])
        # Off, and hosts that are not OpenRouter or OpenAI, do not get a reasoning field.
        local = self.j("post", "/api/agents", {"name": "local", "provider": "openai", "model": "m",
                                               "base_url": "http://127.0.0.1:11434/v1"}).json()
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"choices": [{"message": {"content": "hi"}}]})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                  {"message": "b", "agent_id": local["id"], "thinking": "high"}))
            stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                  {"message": "c", "thinking": "nope"}))
        self.assertNotIn("reasoning", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("reasoning", post.call_args_list[1].kwargs["json"])

    def test_anthropic_thinking_budget(self):
        self.j("post", "/api/agents", {"name": "claude", "provider": "anthropic", "model": "claude",
                                       "base_url": "https://api.anthropic.com", "api_key": "k", "temperature": 0})
        sid = self._session()
        block = {"type": "thinking", "thinking": "plan the edit", "signature": "sig"}
        reply = {"content": [block, {"type": "text", "text": "Done."}]}
        with mock.patch("editor.agents.requests.post", return_value=FakeResp(reply)) as post:
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat",
                                          {"message": "edit", "thinking": "low"}))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 2048})
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertNotIn("temperature", payload)
        self.assertEqual([e["content"] for e in events if e["type"] == "thinking"], ["plan the edit"])

    def test_model_override_per_request(self):
        r = self.j("post", "/api/agents", {"name": "or", "provider": "openai", "model": "default/model",
                                           "base_url": "http://fake/v1"})
        sid = self._session()
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"choices": [{"message": {"content": "hi"}}]})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "a", "model": "other/model"}))
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "b"}))
        self.assertEqual(post.call_args_list[0].kwargs["json"]["model"], "other/model")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["model"], "default/model")
        self.assertEqual(Agent.objects.get(pk=r.json()["id"]).model, "default/model")

    def test_picked_model_is_saved_on_the_agent(self):
        first = self.j("post", "/api/agents", {"name": "or", "provider": "openai", "model": "default/model",
                                               "base_url": "http://fake/v1", "api_key": "k"}).json()
        self.assertTrue(first["is_default"])
        r = self.j("post", f"/api/agents/{first['id']}/model", {"model": " other/model "})
        self.assertEqual(r.json()["model"], "other/model")
        agent = Agent.objects.get(pk=first["id"])
        # only the model changes: key, URL and default flag are kept
        self.assertEqual((agent.model, agent.api_key, agent.base_url, agent.is_default),
                         ("other/model", "k", "http://fake/v1", True))
        boot = self.client.get("/api/bootstrap").json()
        self.assertEqual(boot["agents"][0]["model"], "other/model")  # what the UI shows after a restart
        self.assertEqual(self.j("post", f"/api/agents/{first['id']}/model", {"model": " "}).status_code, 400)
        cli = self.j("post", "/api/agents", {"name": "cli", "provider": "cli", "command": "echo"}).json()
        self.assertEqual(self.j("post", f"/api/agents/{cli['id']}/model", {"model": "m"}).status_code, 400)
        self.assertEqual(Agent.objects.get(pk=first["id"]).model, "other/model")

    def test_picked_agent_is_saved_as_the_default(self):
        a = self.j("post", "/api/agents", {"name": "a", "provider": "openai", "model": "m", "api_key": "k"}).json()
        b = self.j("post", "/api/agents", {"name": "b", "provider": "cli", "command": "echo"}).json()
        self.assertEqual((a["is_default"], b["is_default"]), (True, False))
        r = self.j("post", f"/api/agents/{b['id']}/default")
        self.assertTrue(r.json()["is_default"])
        boot = self.client.get("/api/bootstrap").json()  # what the UI selects after a restart
        self.assertEqual([x["name"] for x in boot["agents"] if x["is_default"]], ["b"])
        self.assertEqual(Agent.objects.get(pk=a["id"]).api_key, "k")
        sid = self._session()  # new chats use the picked agent
        self.assertEqual(self.client.get(f"/api/sessions/{sid}").json()["session"]["agent_id"], b["id"])
        self.assertEqual(self.j("post", "/api/agents/99999/default").status_code, 404)

    def test_list_models(self):
        data = {"data": [
            {"id": "b/model", "supported_parameters": ["tools", "temperature"]},
            {"id": "a/no-tools", "supported_parameters": ["temperature"]},
            {"id": "plain"},
        ]}
        with mock.patch("editor.agents.requests.get", return_value=FakeResp(data)) as get:
            r = self.j("post", "/api/models", {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
                                               "api_key": "k"})
        self.assertEqual(get.call_args.args[0], "https://openrouter.ai/api/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer k")
        self.assertEqual([(m["id"], m["tools"]) for m in r.json()["models"]],
                         [("a/no-tools", False), ("b/model", True), ("plain", None)])
        # listing models by agent_id alone uses the saved agent's base_url
        ollama = self.j("post", "/api/agents", {"name": "ollama", "provider": "openai",
                                                "base_url": "http://localhost:11434/v1", "model": "qwen2.5-coder:7b"}).json()
        with mock.patch("editor.agents.requests.get", return_value=FakeResp(data)) as get:
            self.j("post", "/api/models", {"agent_id": ollama["id"]})
        self.assertEqual(get.call_args.args[0], "http://localhost:11434/v1/models")
        r = self.j("post", "/api/models", {"provider": "webhook"})
        self.assertEqual(r.status_code, 400)

    def test_agent_http_error_reported(self):
        self.j("post", "/api/agents", {"name": "gpt", "provider": "openai", "model": "m"})
        sid = self._session()
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"error": {"message": "bad key"}}, 401)):
            events = stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "hi"}))
        err = [e for e in events if e["type"] == "error"][0]
        self.assertIn("bad key", err["message"])
        self.assertEqual(events[-1]["type"], "done")

    def test_agent_tools_cannot_escape_workspace(self):
        from .fsops import WorkspaceFS
        from .tools import ToolContext
        ctx = ToolContext(WorkspaceFS(self.root))
        ok, msg = ctx.execute("write_file", {"path": "../escape.txt", "content": "x"})
        self.assertFalse(ok)
        self.assertFalse((self.root.parent / "escape.txt").exists())
        ok, _ = ctx.execute("edit_file", {"path": "hello.py", "old_string": "nope", "new_string": "x"})
        self.assertFalse(ok)

    def test_add_folder_to_workspace(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / "b.txt").write_bytes(b"bee\n")
        r = self.j("post", f"/api/workspaces/{self.ws}/folders", {"path": str(other)})
        self.assertEqual(r.status_code, 200)
        folders = r.json()["folders"]
        self.assertEqual(len(folders), 2)
        tree = self.client.get(f"/api/ws/{self.ws}/tree").json()["entries"]
        self.assertEqual({e["name"] for e in tree}, {folders[0]["name"], folders[1]["name"]})
        self.assertTrue(all(e.get("root") for e in tree))
        name = folders[1]["name"]
        content = self.client.get(f"/api/ws/{self.ws}/file", {"path": f"{name}/b.txt"}).json()["content"]
        self.assertEqual(content, "bee\n")
        r = self.j("delete", f"/api/workspaces/{self.ws}/folders", {"name": name})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["folders"]), 1)

    def test_can_remove_primary_folder_when_its_path_is_already_used(self):
        # Workspace A is the temp root plus `other`. Workspace B is only `other`.
        # Removing the temp root from A used to fail: its saved path would become
        # `other`, which B already owns.
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        b = self.j("post", "/api/workspaces", {"path": str(other)}).json()
        added = self.j("post", f"/api/workspaces/{self.ws}/folders", {"path": str(other)}).json()
        primary = added["folders"][0]["name"]
        r = self.j("delete", f"/api/workspaces/{self.ws}/folders", {"name": primary})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([f["name"] for f in r.json()["folders"]], [added["folders"][1]["name"]])
        self.assertEqual(r.json()["path"], str(other.resolve()))
        still = self.client.get("/api/bootstrap").json()["workspaces"]
        self.assertTrue(any(w["id"] == b["id"] for w in still))

    def test_model_can_read_write_update_in_added_folder(self):
        from . import agents
        from .fsops import WorkspaceFS
        from .tools import ToolContext
        other = Path(self.tmp.name) / "extra"
        other.mkdir()
        (other / "notes.txt").write_bytes(b"one\n")
        ws = Workspace.objects.get(pk=self.ws)
        ws.add_folder(str(other))
        ws.save()
        fs = WorkspaceFS(folders=ws.get_folders())
        ctx = ToolContext(fs)
        names = [n for n, _ in fs.folders]
        extra = names[1]

        ok, info = ctx.execute("workspace_info", {})
        self.assertTrue(ok)
        self.assertIn(str(other.resolve()), info)
        self.assertIn(f"{extra}/", info)

        ok, text = ctx.execute("read_file", {"path": f"{extra}/notes.txt"})
        self.assertTrue(ok)
        self.assertEqual(text, "one\n")
        ok, _ = ctx.execute("write_file", {"path": f"{extra}/new/created.txt", "content": "hi"})
        self.assertTrue(ok)
        self.assertEqual((other / "new" / "created.txt").read_text(), "hi")
        ok, _ = ctx.execute("edit_file", {"path": f"{extra}/notes.txt", "old_string": "one", "new_string": "two"})
        self.assertTrue(ok)
        self.assertEqual((other / "notes.txt").read_text(), "two\n")
        # still confined to the added folders
        ok, _ = ctx.execute("write_file", {"path": f"{extra}/../../escape.txt", "content": "x"})
        self.assertFalse(ok)

        agent = Agent(name="t", provider=Agent.OPENAI, model="m")
        prompt = agents.build_system_prompt(agent, fs)
        self.assertIn(str(other.resolve()), prompt)
        self.assertIn(str(self.root.resolve()), prompt)
        self.assertIn(str(other.resolve()), agents.build_context(fs))

    def test_single_folder_workspace_info(self):
        from .fsops import WorkspaceFS
        from .tools import ToolContext
        ok, info = ToolContext(WorkspaceFS(self.root)).execute("workspace_info", {})
        self.assertTrue(ok)
        self.assertIn(str(self.root.resolve()), info)

    def test_analytics_profiles_groups_and_rejects_writes(self):
        import sqlite3
        from openpyxl import Workbook
        (self.root / "sales.csv").write_text("region,amount\nNorth,10\nNorth,5\nSouth,7\n", encoding="utf-8")
        prof = self.j("post", f"/api/ws/{self.ws}/analyze", {"op": "profile", "path": "sales.csv"})
        self.assertEqual(prof.status_code, 200, prof.content)
        amount = next(c for c in prof.json()["columns"] if c["name"] == "amount")
        self.assertEqual(prof.json()["row_count"], 3)
        self.assertEqual(amount["sum"], 22)
        grouped = self.j("post", f"/api/ws/{self.ws}/analyze", {
            "op": "query", "path": "sales.csv", "group_by": ["region"],
            "metrics": [{"column": "amount", "fn": "sum"}],
        })
        self.assertEqual(
            {row["region"]: row["sum_amount"] for row in grouped.json()["rows"]},
            {"North": 15, "South": 7},
        )
        wb = Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["region", "amount"])
        ws.append(["North", 10])
        ws.append(["South", 4])
        wb.save(self.root / "book.xlsx")
        sheet = self.j("post", f"/api/ws/{self.ws}/analyze", {"op": "profile", "path": "book.xlsx", "sheet": "Sales"})
        self.assertEqual(sheet.json()["row_count"], 2)
        self.assertEqual(next(c["sum"] for c in sheet.json()["columns"] if c["name"] == "amount"), 14)

        db = self.root / "books.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table sales (region text, amount int)")
        con.execute("insert into sales values ('North', 2), ('South', 3)")
        con.commit()
        con.close()
        total = self.j("post", f"/api/ws/{self.ws}/analyze", {
            "op": "sql", "path": "books.sqlite", "sql": "SELECT sum(amount) AS total FROM sales",
        })
        self.assertEqual(total.status_code, 200, total.content)
        self.assertEqual(total.json()["rows"][0]["total"], 5)
        denied = self.j("post", f"/api/ws/{self.ws}/analyze", {
            "op": "sql", "path": "books.sqlite", "sql": "DROP TABLE sales",
        })
        self.assertEqual(denied.status_code, 400)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("select count(*) from sales").fetchone()[0], 2)
        con.close()

    def test_analytics_reads_invoice_and_financial_statement(self):
        from docx import Document
        (self.root / "inv.pdf").write_bytes(_pdf_bytes("Invoice No: INV-9 Grand Total: 107.00"))
        invoice = self.j("post", f"/api/ws/{self.ws}/analyze", {"op": "extract", "path": "inv.pdf"})
        self.assertEqual(invoice.status_code, 200, invoice.content)
        fields = {f["name"]: f["value"] for f in invoice.json()["fields"]}
        self.assertEqual(fields["invoice_no"], "INV-9")
        self.assertEqual(fields["total"], 107)
        doc = Document()
        doc.add_paragraph("Income statement")
        doc.add_paragraph("Revenue 1,000")
        doc.add_paragraph("Net income 250")
        doc.save(self.root / "stmt.docx")
        statement = self.j("post", f"/api/ws/{self.ws}/analyze", {"op": "extract", "path": "stmt.docx"})
        values = {f["name"]: f["value"] for f in statement.json()["fields"]}
        self.assertEqual(statement.json()["document_kind"], "financial")
        self.assertEqual(values["revenue"], 1000)
        self.assertEqual(values["net_income"], 250)
        (self.root / "shot.png").write_bytes(_png_bytes())
        with mock.patch("editor.ocr.recognize_bytes", return_value="Receipt\nTotal: 50.00"):
            with mock.patch("editor.ocr.endpoint", return_value="http://ocr.local/read"):
                image = self.j("post", f"/api/ws/{self.ws}/analyze", {"op": "extract", "path": "shot.png"})
        self.assertEqual(image.status_code, 200, image.content)
        self.assertEqual({f["name"]: f["value"] for f in image.json()["fields"]}["total"], 50)

    def test_analytics_mode_sends_analysis_tools(self):
        self.j("post", "/api/agents", {"name": "or", "provider": "openai", "model": "m",
                                       "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-abc"})
        sid = self._session()
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"choices": [{"message": {"content": "hi"}}]})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "total?", "analytics": True}))
        payload = post.call_args.kwargs["json"]
        names = [t["function"]["name"] for t in payload["tools"]]
        self.assertIn("query_data", names)
        self.assertIn("extract_document", names)
        self.assertIn("Analytics mode is on", payload["messages"][0]["content"])
        with mock.patch("editor.agents.requests.post",
                        return_value=FakeResp({"choices": [{"message": {"content": "hi"}}]})) as post:
            stream_events(self.j("post", f"/api/sessions/{sid}/chat", {"message": "edit the code"}))
        names = [t["function"]["name"] for t in post.call_args.kwargs["json"]["tools"]]
        self.assertNotIn("query_data", names)
        self.assertIn("read_file", names)
        self.assertIn("generate_image", names)

    def test_generate_image_saves_a_png_and_needs_approval(self):
        from .agents import build_system_prompt
        from .fsops import WorkspaceFS
        from .imagegen import endpoint_for, model_for
        from .tools import ToolContext
        png = _png_bytes()
        agent = Agent(name="img", provider="openai", base_url="https://openrouter.ai/api/v1",
                      api_key="sk-test", model="chat-model", image_model="google/gemini-2.5-flash-image")
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            self.assertTrue(approvals.required("generate_image"))
        self.assertEqual(endpoint_for(agent.base_url), ("https://openrouter.ai/api/v1/images", "openrouter"))
        self.assertEqual(endpoint_for("https://api.openai.com/v1"),
                         ("https://api.openai.com/v1/images/generations", "openai"))
        self.assertEqual(model_for(Agent(provider="openai", base_url="https://api.openai.com/v1", model="m")), "m")
        self.assertEqual(model_for(agent), "google/gemini-2.5-flash-image")
        ctx = ToolContext(WorkspaceFS(self.root), agent)
        body = {"data": [{"b64_json": _b64(png), "media_type": "image/png"}]}
        with mock.patch("editor.imagegen.requests.post", return_value=FakeResp(body)) as post:
            ok, msg = ctx.execute("generate_image", {"prompt": "a red panda in space", "aspect_ratio": "16:9"})
        self.assertTrue(ok, msg)
        saved = self.root / "images" / "a-red-panda-in-space.png"
        self.assertTrue(saved.is_file(), msg)
        self.assertTrue(saved.read_bytes().startswith(b"\x89PNG"))
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["model"], "google/gemini-2.5-flash-image")
        self.assertEqual(sent["aspect_ratio"], "16:9")
        self.assertEqual(sent["output_format"], "png")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")
        with mock.patch("editor.imagegen.requests.post", return_value=FakeResp(body)):
            ok, msg = ctx.execute("generate_image", {"prompt": "a red panda in space"})
        self.assertTrue(ok, msg)
        self.assertTrue((self.root / "images" / "a-red-panda-in-space-2.png").is_file())
        self.assertEqual(saved.read_bytes(), png)
        ok, msg = ctx.execute("generate_image", {"path": "../escape.png", "prompt": "x"})
        self.assertFalse(ok)
        self.assertFalse((self.root.parent / "escape.png").exists())
        with mock.patch("editor.imagegen.requests.post",
                        return_value=FakeResp({"error": {"message": "no credits"}}, 402)):
            ok, msg = ctx.execute("generate_image", {"prompt": "a cat"})
        self.assertFalse(ok)
        self.assertIn("no credits", msg)
        bad = FakeResp({"error": {"message": "output_format is not supported"}}, 400)
        with mock.patch("editor.imagegen.requests.post", side_effect=[bad, FakeResp(body)]) as post:
            ok, msg = ctx.execute("generate_image", {"prompt": "retry me"})
        self.assertTrue(ok, msg)
        self.assertNotIn("output_format", post.call_args_list[1].kwargs["json"])
        bare = Agent(name="nokey", provider="openai", base_url="https://openrouter.ai/api/v1", model="m")
        ok, msg = ToolContext(WorkspaceFS(self.root), bare).execute("generate_image", {"prompt": "x"})
        self.assertFalse(ok)
        self.assertIn("API key", msg)
        claude = Agent(name="c", provider="anthropic", model="claude", api_key="k")
        ok, msg = ToolContext(WorkspaceFS(self.root), claude).execute("generate_image", {"prompt": "x"})
        self.assertFalse(ok)
        self.assertIn("OpenAI-compatible", msg)
        self.assertIn("generate_image", build_system_prompt(agent))
        saved_agent = self.j("post", "/api/agents", {
            "name": "painter", "provider": "openai", "model": "m",
            "base_url": "https://openrouter.ai/api/v1", "image_model": "bytedance-seed/seedream-4.5",
        }).json()
        self.assertEqual(saved_agent["image_model"], "bytedance-seed/seedream-4.5")
        same = self.j("post", "/api/agents", {
            "name": "same-model", "provider": "openai", "model": "chat-model",
            "base_url": "https://openrouter.ai/api/v1", "image_model": "chat-model",
        }).json()
        self.assertEqual(same["image_model"], "")
        self.assertEqual(model_for(Agent.objects.get(pk=same["id"])), "chat-model")
        changed = self.j("post", f"/api/agents/{same['id']}/image-model",
                         {"image_model": "black-forest-labs/flux.2-pro"}).json()
        self.assertEqual(changed["image_model"], "black-forest-labs/flux.2-pro")
        followed = self.j("post", f"/api/agents/{same['id']}/image-model", {"image_model": "chat-model"}).json()
        self.assertEqual(followed["image_model"], "")

    def test_generate_video_saves_a_short_mp4_and_needs_approval(self):
        from .agents import build_system_prompt
        from .fsops import WorkspaceFS
        from .tools import ToolContext, tool_specs
        from .videogen import clip_duration, endpoint_for, model_for
        mp4 = _mp4_bytes()
        agent = Agent(name="clip", provider="openai", base_url="https://openrouter.ai/api/v1",
                      api_key="sk-test", model="chat-model", video_model="google/veo-3.1")
        with self.settings(PSEDITOR_REQUIRE_APPROVAL=True):
            self.assertTrue(approvals.required("generate_video"))
        self.assertIn("generate_video", [t["name"] for t in tool_specs()])
        self.assertEqual(endpoint_for(agent.base_url), ("https://openrouter.ai/api/v1/videos", "openrouter"))
        self.assertEqual(endpoint_for("https://api.openai.com/v1"), ("https://api.openai.com/v1/videos", "openai"))
        self.assertEqual(model_for(Agent(provider="openai", base_url="https://openrouter.ai/api/v1", model="m")),
                         "google/veo-3.1")
        self.assertEqual(model_for(agent), "google/veo-3.1")
        self.assertEqual(clip_duration(30), 8)
        self.assertEqual(clip_duration(5), 5)
        self.assertEqual(clip_duration("3s"), 4)
        ctx = ToolContext(WorkspaceFS(self.root), agent)
        job = "https://openrouter.ai/api/v1/videos/job1"
        submit = FakeResp({"id": "job1", "polling_url": job, "status": "pending"}, 202)
        pending = FakeResp({"id": "job1", "status": "in_progress"})
        done = FakeResp({"id": "job1", "status": "completed", "unsigned_urls": [job + "/content?index=0"]})
        video = FakeResp({}, content=mp4)
        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", return_value=submit) as post, \
                mock.patch("editor.videogen.requests.get", side_effect=[pending, done, video]) as get:
            ok, msg = ctx.execute("generate_video", {
                "prompt": "waves on a beach", "aspect_ratio": "16:9", "duration": 30,
            })
        self.assertTrue(ok, msg)
        saved = self.root / "videos" / "waves-on-a-beach.mp4"
        self.assertTrue(saved.is_file(), msg)
        self.assertEqual(saved.read_bytes(), mp4)
        self.assertIn("8s", msg)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["model"], "google/veo-3.1")
        self.assertEqual(sent["duration"], 8)
        self.assertEqual(sent["aspect_ratio"], "16:9")
        self.assertEqual(sent["resolution"], "720p")
        self.assertFalse(sent["generate_audio"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertTrue(all("openrouter.ai" in c.args[0] for c in get.call_args_list))
        (self.root / "poster.png").write_bytes(_png_bytes())
        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", return_value=submit) as post, \
                mock.patch("editor.videogen.requests.get", side_effect=[done, video]):
            ok, msg = ctx.execute("generate_video", {"prompt": "waves on a beach", "image": "poster.png"})
        self.assertTrue(ok, msg)
        self.assertTrue((self.root / "videos" / "waves-on-a-beach-2.mp4").is_file())
        self.assertIn("frame_images", post.call_args.kwargs["json"])
        bad_poll = FakeResp({
            "id": "job1", "status": "pending",
            "polling_url": "http://169.254.169.254/latest",
        }, 202)
        seen = []

        def track_get(url, **kwargs):
            seen.append(url)
            if "169.254" in url:
                raise AssertionError(url)
            if url.endswith("/content") or "/content?" in url:
                return video
            return done

        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", return_value=bad_poll), \
                mock.patch("editor.videogen.requests.get", side_effect=track_get):
            ok, msg = ctx.execute("generate_video", {"prompt": "safe poll", "path": "videos/safe.mp4"})
        self.assertTrue(ok, msg)
        self.assertTrue(all(u.startswith("https://openrouter.ai/") for u in seen), seen)
        ok, msg = ctx.execute("generate_video", {"path": "../escape.mp4", "prompt": "x"})
        self.assertFalse(ok)
        self.assertFalse((self.root.parent / "escape.mp4").exists())
        with mock.patch("editor.videogen.requests.post", return_value=FakeResp({"error": {"message": "no credits"}}, 402)):
            ok, msg = ctx.execute("generate_video", {"prompt": "a cat running"})
        self.assertFalse(ok)
        self.assertIn("no credits", msg)
        failed = FakeResp({"id": "job1", "status": "failed", "error": "provider busy"})
        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", return_value=submit), \
                mock.patch("editor.videogen.requests.get", return_value=failed):
            ok, msg = ctx.execute("generate_video", {"prompt": "this fails"})
        self.assertFalse(ok)
        self.assertIn("provider busy", msg)
        bad = FakeResp({"error": {"message": "duration must be one of 4, 6, 8"}}, 400)
        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", side_effect=[bad, submit]) as post, \
                mock.patch("editor.videogen.requests.get", side_effect=[done, video]):
            ok, msg = ctx.execute("generate_video", {"prompt": "retry me"})
        self.assertTrue(ok, msg)
        self.assertNotIn("duration", post.call_args_list[1].kwargs["json"])
        with self.settings(PSEDITOR_MAX_VIDEO_BYTES=8):
            with mock.patch("editor.videogen.time.sleep"), \
                    mock.patch("editor.videogen.requests.post", return_value=submit), \
                    mock.patch("editor.videogen.requests.get", side_effect=[done, video]):
                ok, msg = ctx.execute("generate_video", {"prompt": "too big", "path": "videos/big.mp4"})
        self.assertFalse(ok)
        self.assertIn("too large", msg)
        self.assertFalse((self.root / "videos" / "big.mp4").exists())
        bare = Agent(name="nokey-v", provider="openai", base_url="https://openrouter.ai/api/v1", model="m")
        ok, msg = ToolContext(WorkspaceFS(self.root), bare).execute("generate_video", {"prompt": "x"})
        self.assertFalse(ok)
        self.assertIn("API key", msg)
        claude = Agent(name="c-v", provider="anthropic", model="claude", api_key="k")
        ok, msg = ToolContext(WorkspaceFS(self.root), claude).execute("generate_video", {"prompt": "x"})
        self.assertFalse(ok)
        self.assertIn("OpenAI-compatible", msg)
        self.assertIn("generate_video", build_system_prompt(agent))
        openai_agent = Agent(name="sora", provider="openai", base_url="https://api.openai.com/v1",
                             api_key="sk-test", model="gpt")
        with mock.patch("editor.videogen.time.sleep"), \
                mock.patch("editor.videogen.requests.post", return_value=FakeResp({"id": "video_1", "status": "completed"})) as post, \
                mock.patch("editor.videogen.requests.get", return_value=video):
            ok, msg = ToolContext(WorkspaceFS(self.root), openai_agent).execute(
                "generate_video", {"prompt": "a red bus", "duration": 5, "aspect_ratio": "9:16"})
        self.assertTrue(ok, msg)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["model"], "sora-2")
        self.assertEqual(sent["seconds"], "4")
        self.assertEqual(sent["size"], "720x1280")
        self.assertNotIn("duration", sent)
        saved_agent = self.j("post", "/api/agents", {
            "name": "director", "provider": "openai", "model": "m",
            "base_url": "https://openrouter.ai/api/v1", "video_model": "minimax/hailuo-3",
        }).json()
        self.assertEqual(saved_agent["video_model"], "minimax/hailuo-3")
        blank = self.j("post", "/api/agents", {
            "name": "default-clip", "provider": "openai", "model": "m",
            "base_url": "https://openrouter.ai/api/v1", "video_model": "google/veo-3.1",
        }).json()
        self.assertEqual(blank["video_model"], "")
        self.assertEqual(model_for(Agent.objects.get(pk=blank["id"])), "google/veo-3.1")
        picked = self.j("post", f"/api/agents/{blank['id']}/video-model",
                        {"video_model": "alibaba/wan-2.7"}).json()
        self.assertEqual(picked["video_model"], "alibaba/wan-2.7")
        listed = self.j("post", "/api/models", {"agent_id": blank["id"], "kind": "video",
                                                "base_url": "https://api.openai.com/v1"}).json()
        self.assertIn("sora-2", [m["id"] for m in listed["models"]])
        (self.root / "clip.mp4").write_bytes(mp4)
        whole = self.client.get(f"/api/ws/{self.ws}/preview", {"path": "clip.mp4"})
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(whole["Content-Type"], "video/mp4")
        self.assertEqual(whole["Accept-Ranges"], "bytes")
        body = b"".join(whole.streaming_content)
        whole.close()
        self.assertEqual(body, mp4)
        part = self.client.get(f"/api/ws/{self.ws}/preview", {"path": "clip.mp4"}, HTTP_RANGE="bytes=0-7")
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, mp4[:8])
        self.assertTrue(part["Content-Range"].startswith("bytes 0-7/"))
        denied = self.client.get(f"/api/ws/{self.ws}/file", {"path": "clip.mp4"})
        self.assertEqual(denied.status_code, 400)
        self.assertIn("Binary", denied.json()["error"])
