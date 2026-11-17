"""Safe file-system helpers confined to a workspace root (or several roots)."""
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from django.conf import settings


class FsError(Exception):
    """Raised for user-facing file-system problems."""


def _ignored():
    return set(getattr(settings, "PSEDITOR_IGNORED_DIRS", set()))


def _max_bytes():
    return getattr(settings, "PSEDITOR_MAX_FILE_BYTES", 2 * 1024 * 1024)


def folder_label(path):
    """Display name for a folder path (handles drive roots like E:\\)."""
    p = Path(path)
    return p.name or str(p).rstrip("\\/") or str(p)


class WorkspaceFS:
    """File ops for one folder, or several roots (multi-folder workspace).

    Single-root: paths are relative to that folder (unchanged).
    Multi-root: paths are ``RootName/relative/path``; tree ``""`` lists roots.
    """

    def __init__(self, root=None, folders=None):
        if folders is not None:
            parsed = []
            for f in folders:
                if isinstance(f, dict):
                    name, path = f["name"], f["path"]
                else:
                    name, path = f
                p = Path(path).resolve()
                if not p.is_dir():
                    raise FsError(f"Workspace folder does not exist: {p}")
                parsed.append((str(name), p))
            if not parsed:
                raise FsError("Workspace has no folders.")
            self.folders = parsed
        else:
            p = Path(root).resolve()
            if not p.is_dir():
                raise FsError(f"Workspace folder does not exist: {p}")
            self.folders = [(folder_label(p), p)]
        self.multi = len(self.folders) > 1
        self.root = self.folders[0][1]
        self._by_name = {n: p for n, p in self.folders}

    # -- path routing ------------------------------------------------------
    def _split(self, rel=""):
        """Return (absolute_root, relative_inside) for a workspace-relative path."""
        rel = (rel or "").replace("\\", "/").strip("/")
        if not self.multi:
            return self.folders[0][1], rel
        if not rel:
            return None, ""
        name, _, rest = rel.partition("/")
        if name not in self._by_name:
            raise FsError(f"Unknown workspace folder: {name}")
        return self._by_name[name], rest

    def _prefix(self, name, inner):
        inner = (inner or "").replace("\\", "/").strip("/")
        if not self.multi:
            return inner
        return f"{name}/{inner}" if inner else name

    def resolve(self, rel=""):
        raw = (rel or "").strip().strip('"')
        # Absolute local paths (e.g. E:\app\file.py) are allowed when inside a root.
        if raw and (Path(raw).is_absolute() or (len(raw) >= 2 and raw[1] == ":")):
            try:
                return self.resolve(self.rel(raw))
            except (FsError, ValueError):
                raise FsError("Path is outside the workspace.")
        root, rest = self._split(raw)
        if root is None:
            raise FsError("Path is outside the workspace.")
        target = (root / rest).resolve() if rest else root
        if target != root and root not in target.parents:
            raise FsError("Path is outside the workspace.")
        return target

    def rel(self, path, folder_name=None):
        p = Path(path).resolve()
        if folder_name is not None:
            root = self._by_name[folder_name]
            if p == root:
                inner = ""
            else:
                inner = p.relative_to(root).as_posix()
            return self._prefix(folder_name, inner)
        for name, root in self.folders:
            if p == root or root in p.parents:
                if p == root:
                    inner = ""
                else:
                    inner = p.relative_to(root).as_posix()
                return self._prefix(name, inner)
        raise FsError("Path is outside the workspace.")

    def is_root_entry(self, rel):
        rel = (rel or "").replace("\\", "/").strip("/")
        return self.multi and rel in self._by_name

    # -- listing -----------------------------------------------------------
    def tree(self, rel=""):
        rel = (rel or "").replace("\\", "/").strip("/")
        if self.multi and not rel:
            return [{
                "name": n, "path": n, "type": "dir", "ignored": False, "root": True,
            } for n, _ in self.folders]
        root, rest = self._split(rel)
        d = root / rest if rest else root
        d = d.resolve()
        if d != root and root not in d.parents:
            raise FsError("Path is outside the workspace.")
        if not d.is_dir():
            raise FsError("Not a directory.")
        ignored = _ignored()
        entries = []
        try:
            items = list(os.scandir(d))
        except PermissionError:
            raise FsError("Permission denied.")
        name = None
        if self.multi:
            name = rel.split("/", 1)[0]
        for e in items:
            is_dir = e.is_dir(follow_symlinks=False)
            if self.multi:
                path = self._prefix(name, (Path(rest) / e.name).as_posix() if rest else e.name)
            else:
                path = self.rel(e.path)
            entries.append({
                "name": e.name,
                "path": path,
                "type": "dir" if is_dir else "file",
                "ignored": is_dir and e.name in ignored,
            })
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return entries

    def list_files(self, limit=3000, pattern=None):
        """Flat recursive listing of files (skips ignored dirs)."""
        ignored = _ignored()
        out = []
        for name, root in self.folders:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in ignored)
                for f in sorted(filenames):
                    rel = self.rel(Path(dirpath) / f, folder_name=name if self.multi else None)
                    if pattern and not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(f, pattern):
                        continue
                    out.append(rel)
                    if len(out) >= limit:
                        return out
        return out

    # -- read / write ------------------------------------------------------
    def read(self, rel):
        p = self.resolve(rel)
        if not p.is_file():
            raise FsError(f"File not found: {rel}")
        if p.stat().st_size > _max_bytes():
            raise FsError("File is too large to open.")
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            raise FsError("Binary file - cannot be shown in the editor.")
        return raw.decode("utf-8", errors="replace")

    def exists(self, rel):
        try:
            return self.resolve(rel).exists()
        except FsError:
            return False

    def write(self, rel, content):
        if not (rel or "").strip("/\\"):
            raise FsError("Missing file path.")
        if self.is_root_entry(rel):
            raise FsError("Cannot write to a workspace root.")
        p = self.resolve(rel)
        if p.is_dir():
            raise FsError("Path is a directory.")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode("utf-8"))
        except PermissionError:
            raise FsError(f"Permission denied writing: {p}")
        except OSError as exc:
            raise FsError(f"Could not write file: {exc}")

    def mkdir(self, rel):
        if self.is_root_entry(rel):
            raise FsError("That name is already a workspace root.")
        p = self.resolve(rel)
        if p.exists():
            raise FsError("Already exists.")
        p.mkdir(parents=True)

    def create_file(self, rel):
        if self.is_root_entry(rel):
            raise FsError("That name is already a workspace root.")
        p = self.resolve(rel)
        if p.exists():
            raise FsError("Already exists.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")

    def delete(self, rel):
        if self.is_root_entry(rel):
            raise FsError("Cannot delete a workspace root. Remove it from the workspace instead.")
        p = self.resolve(rel)
        roots = {r for _, r in self.folders}
        if p in roots:
            raise FsError("Cannot delete the workspace root.")
        if not p.exists():
            raise FsError("Not found.")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    def rename(self, rel, new_rel):
        if self.is_root_entry(rel) or self.is_root_entry(new_rel):
            raise FsError("Cannot rename a workspace root.")
        src, dst = self.resolve(rel), self.resolve(new_rel)
        roots = {r for _, r in self.folders}
        if src in roots:
            raise FsError("Cannot rename the workspace root.")
        if not src.exists():
            raise FsError("Source not found.")
        if dst.exists():
            raise FsError("Destination already exists.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    # -- search ------------------------------------------------------------
    def search(self, query, regex=False, case=False, glob=None, limit=200):
        if not query:
            return []
        flags = 0 if case else re.IGNORECASE
        try:
            pat = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise FsError(f"Invalid regex: {exc}")
        results = []
        for rel in self.list_files(limit=5000, pattern=glob):
            try:
                p = self.resolve(rel)
                if p.stat().st_size > 1024 * 1024:
                    continue
                raw = p.read_bytes()
            except (OSError, FsError):
                continue
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if m:
                    results.append({
                        "path": rel, "line": i, "col": m.start() + 1,
                        "text": line.strip()[:240],
                    })
                    if len(results) >= limit:
                        return results
        return results

    # -- snapshots (to detect changes made by command-line agents) ---------
    def snapshot(self, max_files=4000, max_size=256 * 1024):
        snap = {}
        for rel in self.list_files(limit=max_files):
            try:
                p = self.resolve(rel)
                if p.stat().st_size > max_size:
                    continue
                raw = p.read_bytes()
                if b"\x00" in raw[:4096]:
                    continue
                snap[rel] = raw.decode("utf-8", errors="replace")
            except (OSError, FsError):
                continue
        return snap

    # -- commands ----------------------------------------------------------
    def run(self, command, cwd="", timeout=None, stdin=None):
        """Run a shell command inside the workspace. Returns (exit_code, output)."""
        timeout = timeout or getattr(settings, "PSEDITOR_COMMAND_TIMEOUT", 60)
        if self.multi and not (cwd or "").strip("/\\"):
            workdir = self.root
        else:
            workdir = self.resolve(cwd)
        if not workdir.is_dir():
            raise FsError("Working directory does not exist.")
        return run_process(command, workdir, timeout, shell=True, stdin=stdin)


def run_process(command, cwd, timeout, shell=False, stdin=None, env=None):
    """Run a process and return (exit_code, combined_output). Kills the tree on timeout."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    full_env = os.environ.copy()
    full_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    # Drop the editor's own Django settings module: commands run by the agents must not
    # force another project in the workspace to import `pseditor`.
    full_env.pop("DJANGO_SETTINGS_MODULE", None)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(
            command, cwd=str(cwd), shell=shell,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=full_env, creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        return 127, f"Failed to start process: {exc}"
    try:
        out, _ = proc.communicate(
            input=stdin.encode("utf-8") if stdin is not None else None,
            timeout=timeout,
        )
        code = proc.returncode
        note = ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, _ = proc.communicate()
        code = 124
        note = f"\n[timed out after {timeout}s]"
    text = (out or b"").decode("utf-8", errors="replace") + note
    return code, text


def _kill_tree(proc):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass
