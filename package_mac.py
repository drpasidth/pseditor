"""Build dist/PSEditor-mac.zip on Windows: the app plus offline macOS wheels.

Run through package_mac.bat. Uses zipfile (not tar) so the .command scripts keep
Unix line endings and the executable bit when unzipped with Finder.
"""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WHEELS = ROOT / "wheels_mac"
OUT = ROOT / "dist" / "PSEditor-mac.zip"
TOP = "PSEditor"

PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
PLATFORMS = [  # Apple silicon and Intel Macs
    ["macosx_11_0_arm64", "macosx_11_0_universal2"],
    ["macosx_10_13_x86_64", "macosx_10_13_universal2"],
]

EXCLUDE_DIRS = {".venv", "venv", "__pycache__", "dist", "wheels", ".git"}
EXCLUDE_FILES = {"db.sqlite3", "db.sqlite3-journal", "install.log", "run.bat", "install.bat",
                 "install.ps1", "package.bat", "package_full.bat", "package_mac.bat", "package_mac.py",
                 "pseditor.ico"}
EXECUTABLE = {".command", ".sh"}


def download_wheels():
    if WHEELS.exists():
        shutil.rmtree(WHEELS)
    WHEELS.mkdir()
    # pywinpty is Windows-only; pip evaluates markers for this (Windows) machine, so drop it here.
    reqs = [line for line in (ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and "pywinpty" not in line]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("\n".join(reqs) + "\n")
    ok = 0
    try:
        for ver in PYTHON_VERSIONS:
            for plats in PLATFORMS:
                cmd = [sys.executable, "-m", "pip", "download", "-q", "--disable-pip-version-check",
                       "-r", tmp.name, "-d", str(WHEELS), "--only-binary=:all:",
                       "--implementation", "cp", "--python-version", ver]
                for p in plats:
                    cmd += ["--platform", p]
                arch = "arm64" if "arm64" in plats[0] else "x86_64"
                if subprocess.run(cmd).returncode == 0:
                    ok += 1
                    print(f"  Python {ver} {arch}: OK")
                else:
                    print(f"  Python {ver} {arch}: some packages have no wheel, skipped.")
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if not ok:
        sys.exit("Could not download packages. This PC needs internet to build the offline bundle.")


def add(zf, path, arc):
    info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
    info.create_system = 3  # Unix, so Finder honours the permission bits
    info.compress_type = zipfile.ZIP_DEFLATED
    data = path.read_bytes()
    if path.suffix in EXECUTABLE:
        data = data.replace(b"\r\n", b"\n")
        info.external_attr = (0o100755 << 16)
    else:
        info.external_attr = (0o100644 << 16)
    zf.writestr(info, data)


def build_zip():
    OUT.parent.mkdir(exist_ok=True)
    OUT.unlink(missing_ok=True)
    count = 0
    with zipfile.ZipFile(OUT, "w") as zf:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts) or path.is_dir():
                continue
            if rel.name in EXCLUDE_FILES or path.suffix == ".pyc":
                continue
            add(zf, path, f"{TOP}/{rel.as_posix()}")
            count += 1
    print(f"\nCreated:\n  {OUT}  ({count} files, {OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    print("Downloading macOS Python packages into wheels_mac\\ ...")
    download_wheels()
    build_zip()
    print("\nOn the Mac: unzip, then double-click PSEditor/install_mac.command "
          "(first time: right-click > Open).")
