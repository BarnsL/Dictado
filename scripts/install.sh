#!/usr/bin/env bash
# install.sh — cross-platform pip-based install for dictado.
#
# Linux / macOS. For Windows use scripts\install.ps1.
#
# What it does:
#   1. Installs the package in editable mode into the user's site-packages.
#   2. Optionally registers the OS-native autostart entry.
#
# Requirements:
#   - Python 3.10+
#   - On Linux: PortAudio dev headers (apt: portaudio19-dev /
#     dnf: portaudio-devel) — pyaudio's wheels need them on some distros.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed or not on PATH." >&2
  exit 1
fi

echo "Installing dictado into the user site-packages..."
python3 -m pip install --user --upgrade pip
python3 -m pip install --user .

echo
echo "Installation complete."
echo
read -r -p "Install autostart entry now? [Y/n] " yn
case "${yn:-Y}" in
  [Yy]*) python3 -m dictado --install-autostart ;;
  *)     echo "Skipped. Run 'dictado --install-autostart' later if you change your mind." ;;
esac

echo
echo "Done. Run 'dictado' to start the daemon."
