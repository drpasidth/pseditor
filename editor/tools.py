"""Tools an agent can call to inspect and modify the workspace."""
import json

from django.conf import settings

from . import ocr
from .fsops import FsError

MAX_RESULT = 20000

TOOL_SPECS = [
    {
        "name": "workspace_info",
        "description": "Show the folders that make up the workspace (name, location on disk, and how to "
                       "address them in paths). The user can add folders at any time.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_files",
        "description": "List files in the workspace (recursive). Optional glob pattern, e.g. '*.py'.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Optional glob filter"}},
        },
    },
    {
        "name": "read_file",
        "description": "Read a file. Paths are relative to the workspace root. "
                       "Text files return their contents. png, jpg, gif, webp and bmp return the text "
                       "read by the OCR endpoint. Optionally limit a text file to a line range (1-based, inclusive).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or fully overwrite a file with the given content. Parent folders are created.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact string in an existing file. old_string must match exactly "
                       "once (include enough context) unless replace_all is true.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file or folder.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "Search file contents for text or a regex. Returns path:line: text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "regex": {"type": "boolean"},
                "glob": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the workspace (tests, installs, builds). "
                       "Returns the exit code and combined output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}},
            "required": ["command"],
        },
    },
]


class ToolContext:
    """Executes tools against a workspace and records originals for undo."""

    def __init__(self, fs):
        self.fs = fs
        self.checkpoint = {}   # rel path -> original text, or None if it did not exist
        self.changed = []      # ordered unique list of changed rel paths

    # -- checkpoint --------------------------------------------------------
    def _remember(self, rel):
        rel = self.fs.rel(self.fs.resolve(rel))
        if rel in self.checkpoint:
            return rel
        p = self.fs.resolve(rel)
        if p.is_file():
            try:
                self.checkpoint[rel] = self.fs.read(rel)
            except FsError:
                self.checkpoint[rel] = None
        elif p.is_dir():
            # remember every text file under the directory
            for sub in self.fs.list_files(limit=5000):
                if sub.startswith(rel + "/"):
                    self._remember(sub)
            return rel
        else:
            self.checkpoint[rel] = None
        return rel

    def _changed(self, rel):
        if rel not in self.changed:
            self.changed.append(rel)

    def absorb_snapshot_diff(self, before, after):
        """Used for command-line agents: derive changes from two snapshots."""
        for rel in sorted(set(before) | set(after)):
            if before.get(rel) != after.get(rel):
                self.checkpoint.setdefault(rel, before.get(rel))
                self._changed(rel)

    # -- dispatch ----------------------------------------------------------
    def execute(self, name, args):
        """Return (ok, text). Never raises."""
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None:
                return False, f"Unknown tool: {name}"
            text = fn(**(args or {}))
            return True, _clip(text)
        except FsError as exc:
            return False, f"Error: {exc}"
        except TypeError as exc:
            return False, f"Error: bad arguments for {name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - report to the model
            return False, f"Error: {exc}"

    # -- tools -------------------------------------------------------------
    def _t_workspace_info(self):
        fs = self.fs
        if fs.multi:
            lines = [f"Multi-folder workspace ({len(fs.folders)} folders). Prefix paths with the folder name."]
            lines += [f"- {n}/  ->  {p}" for n, p in fs.folders]
        else:
            lines = ["Single-folder workspace. Paths are relative to the root.",
                     f"- (root)  ->  {fs.folders[0][1]}"]
        return "\n".join(lines)

    def _t_list_files(self, pattern=None):
        files = self.fs.list_files(limit=1000, pattern=pattern)
        return "\n".join(files) if files else "(no files)"

    def _t_read_file(self, path, start_line=None, end_line=None):
        try:
            text = self.fs.read(path)
        except FsError as exc:
            if "Binary file" not in str(exc):
                raise
            text = self._ocr_image(path)
        if start_line or end_line:
            lines = text.splitlines()
            s = max((start_line or 1) - 1, 0)
            e = end_line or len(lines)
            return "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines[s:e], start=s))
        return text

    def _ocr_image(self, path):
        file_path = self.fs.resolve(path)
        if file_path.suffix.lower() not in ocr.IMAGE_SUFFIXES:
            raise FsError("Binary file - cannot be shown in the editor.")
        if not ocr.endpoint():
            raise FsError("This image has no text view. Set the OCR endpoint with the gear icon to read it.")
        return ocr.recognize_bytes(file_path.read_bytes(), file_path.name).strip() or "(no text found)"

    def _t_write_file(self, path, content):
        rel = self._remember(path)
        existed = self.fs.exists(rel)
        self.fs.write(rel, content)
        self._changed(rel)
        return f"{'Overwrote' if existed else 'Created'} {rel} ({len(content)} chars)"

    def _t_edit_file(self, path, old_string, new_string, replace_all=False):
        text = self.fs.read(path)
        crlf = "\r\n" in text
        if crlf and "\r\n" not in old_string:
            old_string = old_string.replace("\n", "\r\n")
            new_string = new_string.replace("\n", "\r\n")
        count = text.count(old_string)
        if count == 0:
            raise FsError("old_string not found in file. Read the file again and copy the exact text.")
        if count > 1 and not replace_all:
            raise FsError(f"old_string matches {count} times; add context or set replace_all.")
        rel = self._remember(path)
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        self.fs.write(rel, new_text)
        self._changed(rel)
        return f"Edited {rel} ({count if replace_all else 1} replacement(s))"

    def _t_delete_file(self, path):
        rel = self._remember(path)
        self.fs.delete(rel)
        self._changed(rel)
        return f"Deleted {rel}"

    def _t_search(self, query, regex=False, glob=None):
        hits = self.fs.search(query, regex=regex, glob=glob, limit=100)
        if not hits:
            return "No matches."
        return "\n".join(f"{h['path']}:{h['line']}: {h['text']}" for h in hits)

    def _t_run_command(self, command, cwd=""):
        if not getattr(settings, "PSEDITOR_ALLOW_TERMINAL", False):
            raise FsError("Command execution is disabled (PSEDITOR_ALLOW_TERMINAL).")
        before = self.fs.snapshot(max_files=1500)
        code, out = self.fs.run(command, cwd=cwd)
        self.absorb_snapshot_diff(before, self.fs.snapshot(max_files=1500))
        return f"exit code: {code}\n{out}"

    # -- undo --------------------------------------------------------------
    def checkpoint_json(self):
        return json.dumps(self.checkpoint)


def revert(fs, checkpoint):
    """Restore files recorded in a checkpoint. Returns list of touched paths."""
    touched = []
    for rel, original in checkpoint.items():
        try:
            if original is None:
                if fs.exists(rel):
                    fs.delete(rel)
            else:
                fs.write(rel, original)
            touched.append(rel)
        except FsError:
            continue
    return touched


def _clip(text):
    text = text if isinstance(text, str) else str(text)
    if len(text) > MAX_RESULT:
        return text[:MAX_RESULT] + f"\n...[truncated {len(text) - MAX_RESULT} chars]"
    return text
