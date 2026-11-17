#!/bin/bash
# Start PSEditor on macOS in its own desktop window (no web browser).
# Double-click in Finder, or run: bash run_mac.command
# Set PSEDITOR_BROWSER=1 to use the old browser mode instead.
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  echo "PSEditor is not installed yet. Double-click install_mac.command first."
  read -r -p "Press Enter to close" _
  exit 1
fi

echo "Using: $PY"

if [ -z "$PSEDITOR_BROWSER" ] && ! "$PY" -c "import webview" >/dev/null 2>&1; then
  echo "Installing the desktop window component (pywebview)..."
  "$PY" -m pip install -r requirements.txt || PSEDITOR_BROWSER=1
fi

if [ -z "$PSEDITOR_BROWSER" ]; then
  exec "$PY" desktop.py
fi

if ! "$PY" manage.py migrate --noinput; then
  echo "Migration failed."
  read -r -p "Press Enter to close" _
  exit 1
fi

# Open the browser after a short delay so the first page load hits a live server
(sleep 2 && open "http://127.0.0.1:8800/") &

echo
echo "PSEditor running at http://127.0.0.1:8800/"
echo "Press Ctrl+C to stop."
echo
"$PY" manage.py runserver 127.0.0.1:8800
