#!/bin/bash
# =====================================================================
#   Dictado - one-click launcher (macOS)
# =====================================================================
#
#   Save as a .command file and `chmod +x` it. Finder treats .command
#   files as double-clickable terminal launchers. The first run needs a
#   right-click -> Open With -> Terminal to clear Gatekeeper; after that
#   the double-click flow works directly.
#
#   What this script does:
#     1. Picks a Python 3.10+ off PATH (Homebrew, python.org, asdf...).
#     2. Adds a sibling .venv's site-packages to PYTHONPATH if one
#        exists, so dependencies resolve without `source venv/bin/activate`.
#     3. Execs `python -m dictado` so the tray daemon takes over.
#
#   First-run reminders:
#     - macOS will prompt for Microphone, Accessibility, and Input
#       Monitoring permissions in System Settings -> Privacy & Security.
#       Grant all three or the daemon can't capture audio or paste.
#     - The first launch downloads the Whisper weights (~1.5 GB for the
#       default `medium` model).
# =====================================================================

set -e

DICTADO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DICTADO_ROOT"

# --- Find Python 3.10+ on PATH ---
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "(0,0)")
        if [[ "$version" == "(3, 10)" || "$version" == "(3, 11)" || "$version" == "(3, 12)" || "$version" == "(3, 13)" || "$version" == "(3, 14)" ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[Dictado] No Python 3.10+ on PATH."
    echo "          Install via Homebrew (brew install python) or"
    echo "          download from https://www.python.org/downloads/"
    read -n 1 -s -r -p "Press any key to close."
    exit 1
fi

# --- Use a sibling .venv if one is present ---
if [ -d "$DICTADO_ROOT/.venv/lib" ]; then
    SITE=$(echo "$DICTADO_ROOT/.venv/lib/python"*/site-packages | head -n1)
    export PYTHONPATH="$SITE:$DICTADO_ROOT:$PYTHONPATH"
else
    export PYTHONPATH="$DICTADO_ROOT:$PYTHONPATH"
fi

# Hand off to dictado; closing the terminal window will quit the daemon
# unless you've already moved it into the background via the tray menu.
exec "$PYTHON" -m dictado
