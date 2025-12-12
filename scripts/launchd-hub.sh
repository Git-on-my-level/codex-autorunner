#!/usr/bin/env bash
set -euo pipefail

# Create/update and (re)load the macOS launchd LaunchAgent plist for a CAR hub.
#
# This writes the plist and restarts the agent so it takes effect immediately.
#
# Overrides (env):
#   LABEL         LaunchAgent label (default: com.codex.autorunner)
#   LAUNCH_AGENT  Plist path (default: ~/Library/LaunchAgents/${LABEL}.plist)
#   CAR_ROOT      Hub root path (default: ~/car-workspace)
#   HUB_HOST      Host to bind (default: 127.0.0.1)
#   HUB_PORT      Port to bind (default: 4517)
#   HUB_BASE_PATH Base path (default: /car)
#   NVM_BIN       Node bin path to prepend (default: ~/.nvm/versions/node/v22.12.0/bin)
#   LOCAL_BIN     Local bin path to prepend (default: ~/.local/bin)
#   PY39_BIN      Python bin path to prepend (default: ~/Library/Python/3.9/bin)

LABEL="${LABEL:-com.codex.autorunner}"
LAUNCH_AGENT="${LAUNCH_AGENT:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
CAR_ROOT="${CAR_ROOT:-$HOME/car-workspace}"
HUB_HOST="${HUB_HOST:-127.0.0.1}"
HUB_PORT="${HUB_PORT:-4517}"
HUB_BASE_PATH="${HUB_BASE_PATH:-/car}"

NVM_BIN="${NVM_BIN:-$HOME/.nvm/versions/node/v22.12.0/bin}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PY39_BIN="${PY39_BIN:-$HOME/Library/Python/3.9/bin}"

mkdir -p "$(dirname "${LAUNCH_AGENT}")"
mkdir -p "${CAR_ROOT}/.codex-autorunner"

echo "Writing LaunchAgent plist to ${LAUNCH_AGENT}..."
cat > "${LAUNCH_AGENT}" <<EOF
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
    <string>PATH=${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; codex-autorunner hub serve --host ${HUB_HOST} --port ${HUB_PORT} --base-path ${HUB_BASE_PATH} --path ${CAR_ROOT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${CAR_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${CAR_ROOT}/.codex-autorunner/codex-autorunner-hub.log</string>
  <key>StandardErrorPath</key>
  <string>${CAR_ROOT}/.codex-autorunner/codex-autorunner-hub.log</string>
</dict>
</plist>
EOF

echo "Reloading launchd service ${LABEL}..."
launchctl unload -w "${LAUNCH_AGENT}" >/dev/null 2>&1 || true
launchctl load -w "${LAUNCH_AGENT}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "Done."
