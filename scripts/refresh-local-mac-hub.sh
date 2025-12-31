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
#   ENABLE_TELEGRAM_BOT Enable telegram bot LaunchAgent (auto|true|false; default: auto)
#   TELEGRAM_LABEL launchd label for telegram bot (default: ${LABEL}.telegram)
#   TELEGRAM_PLIST_PATH telegram plist path (default: ~/Library/LaunchAgents/${TELEGRAM_LABEL}.plist)
#   TELEGRAM_LOG  telegram stdout/stderr log path (default: <hub_root>/.codex-autorunner/codex-autorunner-telegram.log)
#   PIPX_VENV     pipx venv path (default: ~/.local/pipx/venvs/codex-autorunner)
#   PIPX_PYTHON   python inside venv (default: ${PIPX_VENV}/bin/python)

LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
ENABLE_TELEGRAM_BOT="${ENABLE_TELEGRAM_BOT:-auto}"
TELEGRAM_LABEL="${TELEGRAM_LABEL:-${LABEL}.telegram}"
TELEGRAM_PLIST_PATH="${TELEGRAM_PLIST_PATH:-$HOME/Library/LaunchAgents/${TELEGRAM_LABEL}.plist}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

PIPX_VENV="${PIPX_VENV:-$HOME/.local/pipx/venvs/codex-autorunner}"
PIPX_PYTHON="${PIPX_PYTHON:-$PIPX_VENV/bin/python}"

_plist_arg_value() {
  local key py
  key="$1"
  py="${PIPX_PYTHON}"
  if [[ ! -x "${py}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      py="python3"
    elif command -v python >/dev/null 2>&1; then
      py="python"
    else
      return 0
    fi
  fi
  "${py}" - "$key" "${PLIST_PATH}" <<'PY'
import re
import sys
from pathlib import Path

key = sys.argv[1]
path = Path(sys.argv[2])
try:
    text = path.read_text(encoding="utf-8")
except Exception:
    sys.exit(0)

pattern = re.compile(r"(?:--%s(?:=|\s+))([^\s<]+)" % re.escape(key))
match = pattern.search(text)
if not match:
    sys.exit(0)

value = match.group(1).strip("\"'")
if value:
    sys.stdout.write(value)
PY
}

telegram_state() {
  local root cfg
  if [[ "${ENABLE_TELEGRAM_BOT}" == "1" || "${ENABLE_TELEGRAM_BOT}" == "true" ]]; then
    echo "enabled"
    return 0
  fi
  if [[ "${ENABLE_TELEGRAM_BOT}" == "0" || "${ENABLE_TELEGRAM_BOT}" == "false" ]]; then
    echo "disabled"
    return 0
  fi
  root="$1"
  if [[ -z "${root}" ]]; then
    echo "unknown"
    return 0
  fi
  cfg="${root}/.codex-autorunner/config.yml"
  if [[ ! -f "${cfg}" ]]; then
    echo "unknown"
    return 0
  fi
  if awk '
    BEGIN {in_section=0; found=0}
    /^telegram_bot:/ {in_section=1; next}
    /^[^[:space:]]/ {in_section=0}
    in_section && $1 == "enabled:" && tolower($2) == "true" {found=1}
    END {exit !found}
  ' "${cfg}"; then
    echo "enabled"
  else
    echo "disabled"
  fi
}

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

hub_root="$(_plist_arg_value path)"
telegram_status="$(telegram_state "${hub_root}")"
if [[ "${telegram_status}" == "enabled" ]]; then
  if [[ -z "${hub_root}" ]]; then
    echo "Telegram enabled but unable to derive hub root; skipping telegram LaunchAgent." >&2
  else
    if [[ ! -f "${TELEGRAM_PLIST_PATH}" ]]; then
      telegram_log="${TELEGRAM_LOG:-${hub_root}/.codex-autorunner/codex-autorunner-telegram.log}"
      echo "Writing launchd plist to ${TELEGRAM_PLIST_PATH}..."
      mkdir -p "$(dirname "${TELEGRAM_PLIST_PATH}")"
      mkdir -p "${hub_root}/.codex-autorunner"
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
    <string>${PIPX_VENV}/bin/codex-autorunner telegram start --path ${hub_root}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${hub_root}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${telegram_log}</string>
  <key>StandardErrorPath</key>
  <string>${telegram_log}</string>
</dict>
</plist>
EOF
    fi
    echo "Reloading launchd service ${TELEGRAM_LABEL}..."
    launchctl unload "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
    launchctl load -w "${TELEGRAM_PLIST_PATH}"
    launchctl kickstart -k "gui/$(id -u)/${TELEGRAM_LABEL}"
  fi
elif [[ "${telegram_status}" == "disabled" ]]; then
  if [[ -f "${TELEGRAM_PLIST_PATH}" ]]; then
    echo "Telegram disabled; unloading launchd service ${TELEGRAM_LABEL}..."
    launchctl unload -w "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
  fi
elif [[ -f "${TELEGRAM_PLIST_PATH}" ]]; then
  echo "Reloading launchd service ${TELEGRAM_LABEL}..."
  launchctl unload "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${TELEGRAM_PLIST_PATH}"
  launchctl kickstart -k "gui/$(id -u)/${TELEGRAM_LABEL}"
fi

echo "Done."
