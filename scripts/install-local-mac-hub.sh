#!/usr/bin/env bash
set -euo pipefail

# Opinionated local install for macOS user scope.
# - Installs codex-autorunner via pipx from this repo path.
# - Creates/initializes a hub at ~/car-workspace.
# - Drops a launchd agent plist and loads it to run the hub server.

WORKSPACE="${WORKSPACE:-$HOME/car-workspace}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-4173}"
LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx is required; install via 'python3 -m pip install --user pipx' and re-run." >&2
  exit 1
fi

echo "Installing codex-autorunner from ${PACKAGE_SRC} via pipx..."
pipx install --force "${PACKAGE_SRC}"

PIPX_ROOT="${PIPX_ROOT:-$HOME/.local/pipx}"
PIPX_VENV="${PIPX_VENV:-$PIPX_ROOT/venvs/codex-autorunner}"
CURRENT_VENV_LINK="${CURRENT_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.current}"
if [[ -d "${PIPX_VENV}" ]]; then
  ln -sfn "${PIPX_VENV}" "${CURRENT_VENV_LINK}"
fi

echo "Ensuring hub workspace at ${WORKSPACE}..."
mkdir -p "${WORKSPACE}"
codex-autorunner init --mode hub --path "${WORKSPACE}"

echo "Writing launchd plist to ${PLIST_PATH}..."
mkdir -p "$(dirname "${PLIST_PATH}")"
cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>${CURRENT_VENV_LINK}/bin/codex-autorunner hub serve --host ${HOST} --port ${PORT} --path ${WORKSPACE}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${WORKSPACE}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${WORKSPACE}/.codex-autorunner/codex-autorunner-hub.log</string>
  <key>StandardErrorPath</key>
  <string>${WORKSPACE}/.codex-autorunner/codex-autorunner-hub.log</string>
</dict>
</plist>
EOF

echo "Loading launchd service..."
launchctl unload "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl load -w "${PLIST_PATH}"

echo "Done. Visit http://${HOST}:${PORT} once the service is up."
