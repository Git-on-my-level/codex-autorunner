#!/usr/bin/env bash
set -euo pipefail

# Refresh a launchd-managed local macOS hub to the current repo checkout.
# - Reinstalls this repo into the pipx venv (ensures deps are present)
# - Restarts the launchd LaunchAgent and kickstarts it
#
# Overrides:
#   PACKAGE_SRC   Path to this repo (default: script/..)
#   LABEL         launchd label (default: com.codex.autorunner)
#   PLIST_PATH    launchd plist path (default: ~/Library/LaunchAgents/${LABEL}.plist)
#   PIPX_VENV     pipx venv path (default: ~/.local/pipx/venvs/codex-autorunner)
#   PIPX_PYTHON   python inside venv (default: ${PIPX_VENV}/bin/python)

LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

PIPX_VENV="${PIPX_VENV:-$HOME/.local/pipx/venvs/codex-autorunner}"
PIPX_PYTHON="${PIPX_PYTHON:-$PIPX_VENV/bin/python}"

if [[ ! -f "${PLIST_PATH}" ]]; then
  echo "LaunchAgent plist not found at ${PLIST_PATH}" >&2
  echo "Run scripts/install-local-mac-hub.sh first (or set PLIST_PATH)." >&2
  exit 1
fi

echo "Refreshing codex-autorunner from ${PACKAGE_SRC}..."
if [[ -x "${PIPX_PYTHON}" ]]; then
  "${PIPX_PYTHON}" -m pip install --force-reinstall "${PACKAGE_SRC}"
else
  if ! command -v pipx >/dev/null 2>&1; then
    echo "pipx is required (or set PIPX_PYTHON to an existing venv python)." >&2
    exit 1
  fi
  pipx install --force "${PACKAGE_SRC}"
fi

echo "Reloading launchd service ${LABEL}..."
launchctl unload "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl load -w "${PLIST_PATH}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "Done."
