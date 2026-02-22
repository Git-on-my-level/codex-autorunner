#!/usr/bin/env bash
set -euo pipefail

# Opinionated local install for macOS user scope.
# - Installs codex-autorunner via pipx from this repo path.
# - Creates/initializes a hub at ~/car-workspace.
# - Drops a launchd agent plist and loads it to run the hub server.
#
# Overrides:
#   WORKSPACE              Hub root (default: ~/car-workspace)
#   HOST                   Hub bind host (default: 127.0.0.1)
#   PORT                   Hub bind port (default: 4173)
#   LABEL                  launchd label (default: com.codex.autorunner)
#   PLIST_PATH             launchd plist path (default: ~/Library/LaunchAgents/${LABEL}.plist)
#   ENABLE_TELEGRAM_BOT    Enable telegram bot LaunchAgent (auto|true|false; default: auto)
#   TELEGRAM_LABEL         launchd label for telegram bot (default: ${LABEL}.telegram)
#   TELEGRAM_PLIST_PATH    telegram plist path (default: ~/Library/LaunchAgents/${TELEGRAM_LABEL}.plist)
#   TELEGRAM_LOG           telegram stdout/stderr log path (default: ${WORKSPACE}/.codex-autorunner/codex-autorunner-telegram.log)
#   ENABLE_DISCORD_BOT     Enable discord bot LaunchAgent (auto|true|false; default: auto)
#   DISCORD_LABEL          launchd label for discord bot (default: ${LABEL}.discord)
#   DISCORD_PLIST_PATH     discord plist path (default: ~/Library/LaunchAgents/${DISCORD_LABEL}.plist)
#   DISCORD_LOG            discord stdout/stderr log path (default: ${WORKSPACE}/.codex-autorunner/codex-autorunner-discord.log)
#   NVM_BIN                Node bin path to prepend (default: ~/.nvm/versions/node/v22.12.0/bin)
#   LOCAL_BIN              Local bin path to prepend (default: ~/.local/bin)
#   PY39_BIN               Python bin path to prepend (default: ~/Library/Python/3.9/bin)
#   OPENCODE_BIN           OpenCode bin path to prepend (default: ~/.opencode/bin)

WORKSPACE="${WORKSPACE:-$HOME/car-workspace}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4173}"
LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
ENABLE_TELEGRAM_BOT="${ENABLE_TELEGRAM_BOT:-auto}"
TELEGRAM_LABEL="${TELEGRAM_LABEL:-${LABEL}.telegram}"
TELEGRAM_PLIST_PATH="${TELEGRAM_PLIST_PATH:-$HOME/Library/LaunchAgents/${TELEGRAM_LABEL}.plist}"
TELEGRAM_LOG="${TELEGRAM_LOG:-${WORKSPACE}/.codex-autorunner/codex-autorunner-telegram.log}"
ENABLE_DISCORD_BOT="${ENABLE_DISCORD_BOT:-auto}"
DISCORD_LABEL="${DISCORD_LABEL:-${LABEL}.discord}"
DISCORD_PLIST_PATH="${DISCORD_PLIST_PATH:-$HOME/Library/LaunchAgents/${DISCORD_LABEL}.plist}"
DISCORD_LOG="${DISCORD_LOG:-${WORKSPACE}/.codex-autorunner/codex-autorunner-discord.log}"
NVM_BIN="${NVM_BIN:-$HOME/.nvm/versions/node/v22.12.0/bin}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PY39_BIN="${PY39_BIN:-$HOME/Library/Python/3.9/bin}"
OPENCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

is_loopback_host() {
  case "$1" in
    localhost|127.*|::1)
      return 0
      ;;
  esac
  return 1
}

generate_token() {
  local python_bin="${CURRENT_VENV_LINK}/bin/python"
  if [[ -x "${python_bin}" ]]; then
    "${python_bin}" - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
    return 0
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return 0
  fi
  LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 64
}

set_env_var() {
  local env_path="$1"
  local key="$2"
  local value="$3"
  if [[ -f "${env_path}" ]]; then
    awk -v key="${key}" -v value="${value}" '
      BEGIN {updated=0}
      $0 ~ ("^" key "=") {
        if (!updated) {
          print key "=" value
          updated=1
        }
        next
      }
      {print}
      END {
        if (!updated) {
          print key "=" value
        }
      }
    ' "${env_path}" > "${env_path}.tmp"
    mv "${env_path}.tmp" "${env_path}"
  else
    printf "%s=%s\n" "${key}" "${value}" > "${env_path}"
  fi
}

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

CONFIG_PATH="${WORKSPACE}/.codex-autorunner/config.yml"
ENV_PATH="${WORKSPACE}/.codex-autorunner/.env"
AUTH_TOKEN_ENV_NAME="CAR_SERVER_TOKEN"
AUTH_TOKEN=""

if ! is_loopback_host "${HOST}"; then
  mkdir -p "$(dirname "${ENV_PATH}")"
  if [[ -f "${ENV_PATH}" ]]; then
    AUTH_TOKEN="$(awk -F= -v key="${AUTH_TOKEN_ENV_NAME}" '$1 == key {print $2}' "${ENV_PATH}" | tail -n 1)"
  fi
  if [[ -z "${AUTH_TOKEN}" ]]; then
    AUTH_TOKEN="$(generate_token)"
    set_env_var "${ENV_PATH}" "${AUTH_TOKEN_ENV_NAME}" "${AUTH_TOKEN}"
  fi
fi

if [[ -x "${CURRENT_VENV_LINK}/bin/python" ]]; then
  "${CURRENT_VENV_LINK}/bin/python" - "${CONFIG_PATH}" "${HOST}" "${PORT}" "${AUTH_TOKEN_ENV_NAME}" "${AUTH_TOKEN}" <<'PY'
from __future__ import annotations

from pathlib import Path
import sys

import yaml

config_path = Path(sys.argv[1])
host = sys.argv[2]
port = int(sys.argv[3])
auth_env_name = sys.argv[4]
auth_token = sys.argv[5]

data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
server = data.setdefault("server", {})
server["host"] = host
server["port"] = port
if auth_token:
    if not server.get("auth_token_env"):
        server["auth_token_env"] = auth_env_name
    if not server.get("allowed_hosts"):
        server["allowed_hosts"] = ["*"]

config_path.write_text(
    yaml.safe_dump(data, sort_keys=False),
    encoding="utf-8",
)
PY
else
  echo "Warning: ${CURRENT_VENV_LINK}/bin/python not found; skipping config update." >&2
fi

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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${CURRENT_VENV_LINK}/bin/codex-autorunner hub serve --host ${HOST} --port ${PORT} --path ${WORKSPACE}</string>
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

discord_config_values() {
  local cfg
  cfg="$1"
  if [[ ! -f "${cfg}" ]]; then
    echo "false CAR_DISCORD_BOT_TOKEN CAR_DISCORD_APP_ID"
    return 0
  fi
  awk '
    BEGIN {
      in_section=0
      enabled="false"
      bot_env="CAR_DISCORD_BOT_TOKEN"
      app_env="CAR_DISCORD_APP_ID"
    }
    /^discord_bot:/ {in_section=1; next}
    /^[^[:space:]]/ {in_section=0}
    !in_section {next}
    $1 == "enabled:" && tolower($2) == "true" {enabled="true"}
    $1 == "bot_token_env:" {
      bot_env=$2
      gsub(/["'"'"' ]/, "", bot_env)
    }
    $1 == "app_id_env:" {
      app_env=$2
      gsub(/["'"'"' ]/, "", app_env)
    }
    END {
      print enabled, bot_env, app_env
    }
  ' "${cfg}"
}

env_var_is_set() {
  local name
  name="$1"
  if [[ ! "${name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    return 1
  fi
  [[ -n "${!name:-}" ]]
}

discord_enabled() {
  local cfg enabled bot_env app_env
  cfg="${WORKSPACE}/.codex-autorunner/config.yml"
  read -r enabled bot_env app_env <<<"$(discord_config_values "${cfg}")"
  if [[ "${ENABLE_DISCORD_BOT}" == "0" || "${ENABLE_DISCORD_BOT}" == "false" ]]; then
    return 1
  fi
  if [[ "${ENABLE_DISCORD_BOT}" == "1" || "${ENABLE_DISCORD_BOT}" == "true" ]]; then
    enabled="true"
  fi
  if [[ "${enabled}" != "true" ]]; then
    return 1
  fi
  if ! env_var_is_set "${bot_env}"; then
    echo "Discord enabled but env var ${bot_env} is unset; skipping launchd service." >&2
    return 1
  fi
  if ! env_var_is_set "${app_env}"; then
    echo "Discord enabled but env var ${app_env} is unset; skipping launchd service." >&2
    return 1
  fi
  return 0
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${CURRENT_VENV_LINK}/bin/codex-autorunner telegram start --path ${WORKSPACE}</string>
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

if discord_enabled; then
  echo "Writing launchd plist to ${DISCORD_PLIST_PATH}..."
  mkdir -p "$(dirname "${DISCORD_PLIST_PATH}")"
  cat > "${DISCORD_PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${DISCORD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-lc</string>
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${CURRENT_VENV_LINK}/bin/codex-autorunner discord start --path ${WORKSPACE}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${WORKSPACE}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DISCORD_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${DISCORD_LOG}</string>
</dict>
</plist>
EOF

  echo "Loading launchd service ${DISCORD_LABEL}..."
  launchctl unload "${DISCORD_PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${DISCORD_PLIST_PATH}"
elif [[ -f "${DISCORD_PLIST_PATH}" ]]; then
  echo "Discord disabled; unloading launchd service ${DISCORD_LABEL}..."
  launchctl unload "${DISCORD_PLIST_PATH}" >/dev/null 2>&1 || true
fi

if [[ -n "${AUTH_TOKEN}" ]]; then
  echo "Auth token stored in ${ENV_PATH}; keep it private."
fi
echo "Done. Visit http://${HOST}:${PORT} once the service is up."
