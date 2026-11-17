#!/bin/bash
# PSEditor installer for macOS. Double-click this file in Finder
# (first time: right-click > Open), or run:  bash install_mac.command
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
LOG="$ROOT/install.log"
exec > >(tee "$LOG") 2>&1

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
fail() {
  printf '\n\033[31m%s\033[0m\n' "$1"
  echo "Details were saved to $LOG"
  read -r -p "Press Enter to close" _
  exit 1
}

echo "PSEditor installer (macOS)"
echo "Folder: $ROOT"

for f in manage.py requirements.txt run_mac.command; do
  [ -f "$ROOT/$f" ] || fail "Missing $f. Unzip the full PSEditor folder, then run install_mac.command from inside it."
done

# Files from a downloaded zip are quarantined; clear it so run_mac.command opens without warnings.
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
chmod +x "$ROOT"/*.command 2>/dev/null || true

# /usr/bin/python3 is a stub that pops up an Xcode tools dialog when the tools are not installed.
py_ok() {
  if [ "$1" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then return 1; fi
  [ -x "$1" ] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

find_python() {
  local c
  for c in \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    "$(command -v python3.13 2>/dev/null)" "$(command -v python3 2>/dev/null)"; do
    if [ -n "$c" ] && py_ok "$c"; then echo "$c"; return 0; fi
  done
  return 1
}

step "Looking for Python 3.10 or newer"
PY="$(find_python)"
if [ -z "$PY" ] && command -v brew >/dev/null 2>&1; then
  read -r -p "Python 3.10+ was not found. Install Python 3.13 with Homebrew now? [y/N] " ans
  if [[ "$ans" =~ ^[Yy] ]]; then
    step "Installing Python 3.13 (Homebrew)"
    brew install python@3.13 || fail "Homebrew could not install Python."
    PY="$(find_python)"
  fi
fi
[ -n "$PY" ] || fail "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/macos/ (or: brew install python@3.13), then run install_mac.command again."
echo "Using Python $("$PY" -c 'import sys; print(sys.version.split()[0])')"
echo "$PY"

VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
if py_ok "$VPY"; then
  step "Reusing the existing virtual environment"
else
  step "Creating virtual environment"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV" || fail "Creating the virtual environment failed."
fi

step "Installing Python packages"
installed=0
if ls "$ROOT"/wheels_mac/*.whl >/dev/null 2>&1; then
  echo "Installing offline from $ROOT/wheels_mac"
  if "$VPY" -m pip install --no-index --find-links "$ROOT/wheels_mac" -r "$ROOT/requirements.txt"; then
    installed=1
  else
    echo "The bundled packages do not cover this Python version. Trying the internet..."
  fi
fi
if [ "$installed" = 0 ]; then
  "$VPY" -m pip install -r "$ROOT/requirements.txt" || fail "Could not download the Python packages. This Mac cannot reach pypi.org (no internet, DNS, or a proxy is required). Behind a proxy: export HTTPS_PROXY=http://proxy:port, then run: bash install_mac.command"
fi
"$VPY" -c "import django, requests" || fail "Checking Django and requests failed."

step "Preparing the database"
"$VPY" "$ROOT/manage.py" migrate --noinput || fail "Database migration failed."

AGENT_PY=$(cat <<'EOF'
import os, sys, django
sys.path.insert(0, os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pseditor.settings')
django.setup()
from editor.models import Agent
a = Agent.objects.filter(is_default=True).first() or Agent.objects.first()
key = os.environ.get('PSEDITOR_OPENROUTER_KEY', '').strip()
if a is None or 'openrouter.ai' not in a.base_url:
    print('skip')
elif key:
    a.api_key = key
    a.save(update_fields=['api_key'])
    print('saved')
else:
    print('has-key' if a.api_key else 'no-key')
EOF
)
state="$(PSEDITOR_OPENROUTER_KEY= "$VPY" -c "$AGENT_PY" | tail -n 1)"
if [ "$state" = "no-key" ]; then
  step "OpenRouter agent (default)"
  echo "Paste your OpenRouter API key (https://openrouter.ai/keys), or press Enter to add it later in Agents."
  read -r -s -p "OpenRouter API key: " key; echo
  if [ -n "${key// /}" ]; then
    state="$(PSEDITOR_OPENROUTER_KEY="$key" "$VPY" -c "$AGENT_PY" | tail -n 1)"
    [ "$state" = "saved" ] && echo "API key saved." || echo "Could not save the key; add it later in Agents."
  else
    echo "Skipped. Add the key later: click Agents > OpenRouter > API key > Save."
  fi
  key=""
fi

step "Creating a Desktop launcher"
LAUNCHER="$HOME/Desktop/PSEditor.command"
printf '#!/bin/bash\nexec "%s/run_mac.command"\n' "$ROOT" > "$LAUNCHER" && chmod +x "$LAUNCHER" && echo "Launcher: $LAUNCHER"

printf '\n\033[32mPSEditor is installed.\033[0m\n'
echo "Start it with PSEditor.command on the Desktop, or run_mac.command in this folder."
echo "Then open http://127.0.0.1:8800/ in a browser."
echo "The first page load needs internet so Monaco and xterm.js can load from the CDN."
echo
read -r -p "Start PSEditor now? [y/N] " ans
if [[ "$ans" =~ ^[Yy] ]]; then
  exec "$ROOT/run_mac.command"
fi
