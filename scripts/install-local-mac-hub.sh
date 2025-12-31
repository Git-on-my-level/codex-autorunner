#!/usr/bin/env bash
set -euo pipefail

# Opinionated local install for macOS user scope.
# - Installs codex-autorunner via pipx from this repo path.
# - Creates/initializes a hub at ~/car-workspace.
# - Drops a launchd agent plist and loads it to run the hub server.
#
# Overrides:
#   WORKSPACE              Hub root (default: ~/car-workspace)
#   HOST                   Hub bind host (default: 0.0.0.0)
#   PORT                   Hub bind port (default: 4173)
#   LABEL                  launchd label (default: com.codex.autorunner)
#   PLIST_PATH             launchd plist path (default: ~/Library/LaunchAgents/${LABEL}.plist)
#   ENABLE_TELEGRAM_BOT    Enable telegram bot LaunchAgent (auto|true|false; default: auto)
#   TELEGRAM_LABEL         launchd label for telegram bot (default: ${LABEL}.telegram)
#   TELEGRAM_PLIST_PATH    telegram plist path (default: ~/Library/LaunchAgents/${TELEGRAM_LABEL}.plist)
#   TELEGRAM_LOG           telegram stdout/stderr log path (default: ${WORKSPACE}/.codex-autorunner/codex-autorunner-telegram.log)

WORKSPACE="${WORKSPACE:-$HOME/car-workspace}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-4173}"
LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
ENABLE_TELEGRAM_BOT="${ENABLE_TELEGRAM_BOT:-auto}"
TELEGRAM_LABEL="${TELEGRAM_LABEL:-${LABEL}.telegram}"
TELEGRAM_PLIST_PATH="${TELEGRAM_PLIST_PATH:-$HOME/Library/LaunchAgents/${TELEGRAM_LABEL}.plist}"
TELEGRAM_LOG="${TELEGRAM_LOG:-${WORKSPACE}/.codex-autorunner/codex-autorunner-telegram.log}"

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

telegram_enabled() {
  if [[ "${ENABLE_TELEGRAM_BOT}" == "1" || "${ENABLE_TELEGRAM_BOT}" == "true" ]]; then
    return 0
  fi
  if [[ "${ENABLE_TELEGRAM_BOT}" == "0" || "${ENABLE_TELEGRAM_BOT}" == "false" ]]; then
    return 1
  fi
  local cfg
  cfg="${WORKSPACE}/.codex-autorunner/config.yml"
  if [[ ! -f "${cfg}" ]]; then
    return 1
  fi
  awk '
    BEGIN {in_section=0; found=0}
    /^telegram_bot:/ {in_section=1; next}
    /^[^[:space:]]/ {in_section=0}
    in_section && $1 == "enabled:" && tolower($2) == "true" {found=1}
    END {exit !found}
  ' "${cfg}"
}

if telegram_enabled; then
  echo "Writing launchd plist to ${TELEGRAM_PLIST_PATH}..."
  mkdir -p "$(dirname "${TELEGRAM_PLIST_PATH}")"
  cat > "${TELEGRAM_PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${TELEGRAM_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>${CURRENT_VENV_LINK}/bin/codex-autorunner telegram start --path ${WORKSPACE}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${WORKSPACE}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${TELEGRAM_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${TELEGRAM_LOG}</string>
</dict>
</plist>
EOF

  echo "Loading launchd service ${TELEGRAM_LABEL}..."
  launchctl unload "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${TELEGRAM_PLIST_PATH}"
fi

echo "Done. Visit http://${HOST}:${PORT} once the service is up."
