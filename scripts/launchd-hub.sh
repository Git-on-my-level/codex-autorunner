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
#   ENABLE_TELEGRAM_BOT Enable telegram bot LaunchAgent (auto|true|false; default: auto)
#   TELEGRAM_LABEL LaunchAgent label for telegram bot (default: ${LABEL}.telegram)
#   TELEGRAM_LAUNCH_AGENT Telegram plist path (default: ~/Library/LaunchAgents/${TELEGRAM_LABEL}.plist)
#   TELEGRAM_LOG  Telegram stdout/stderr log path (default: ${CAR_ROOT}/.codex-autorunner/codex-autorunner-telegram.log)
#   ENABLE_DISCORD_BOT Enable discord bot LaunchAgent (auto|true|false; default: auto)
#   DISCORD_LABEL LaunchAgent label for discord bot (default: ${LABEL}.discord)
#   DISCORD_LAUNCH_AGENT Discord plist path (default: ~/Library/LaunchAgents/${DISCORD_LABEL}.plist)
#   DISCORD_LOG   Discord stdout/stderr log path (default: ${CAR_ROOT}/.codex-autorunner/codex-autorunner-discord.log)
#   NVM_BIN       Node bin path to prepend (default: ~/.nvm/versions/node/v22.12.0/bin)
#   LOCAL_BIN     Local bin path to prepend (default: ~/.local/bin)
#   PY39_BIN      Python bin path to prepend (default: ~/Library/Python/3.9/bin)
#   OPENCODE_BIN  OpenCode bin path to prepend (default: ~/.opencode/bin)
#   HUB_BIN       Full path to codex-autorunner binary (default: ~/.local/pipx/venvs/codex-autorunner.current/bin/codex-autorunner)

LABEL="${LABEL:-com.codex.autorunner}"
LAUNCH_AGENT="${LAUNCH_AGENT:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
CAR_ROOT="${CAR_ROOT:-$HOME/car-workspace}"
HUB_HOST="${HUB_HOST:-127.0.0.1}"
HUB_PORT="${HUB_PORT:-4517}"
HUB_BASE_PATH="${HUB_BASE_PATH:-/car}"
ENABLE_TELEGRAM_BOT="${ENABLE_TELEGRAM_BOT:-auto}"
TELEGRAM_LABEL="${TELEGRAM_LABEL:-${LABEL}.telegram}"
TELEGRAM_LAUNCH_AGENT="${TELEGRAM_LAUNCH_AGENT:-$HOME/Library/LaunchAgents/${TELEGRAM_LABEL}.plist}"
TELEGRAM_LOG="${TELEGRAM_LOG:-${CAR_ROOT}/.codex-autorunner/codex-autorunner-telegram.log}"
ENABLE_DISCORD_BOT="${ENABLE_DISCORD_BOT:-auto}"
DISCORD_LABEL="${DISCORD_LABEL:-${LABEL}.discord}"
DISCORD_LAUNCH_AGENT="${DISCORD_LAUNCH_AGENT:-$HOME/Library/LaunchAgents/${DISCORD_LABEL}.plist}"
DISCORD_LOG="${DISCORD_LOG:-${CAR_ROOT}/.codex-autorunner/codex-autorunner-discord.log}"

NVM_BIN="${NVM_BIN:-$HOME/.nvm/versions/node/v22.12.0/bin}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PY39_BIN="${PY39_BIN:-$HOME/Library/Python/3.9/bin}"
OPENCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin}"
HUB_BIN="${HUB_BIN:-$HOME/.local/pipx/venvs/codex-autorunner.current/bin/codex-autorunner}"

if [[ ! -x "${HUB_BIN}" ]]; then
  fallback="$HOME/.local/pipx/venvs/codex-autorunner/bin/codex-autorunner"
  if [[ -x "${fallback}" ]]; then
    mkdir -p "$HOME/.local/pipx/venvs"
    ln -sfn "$HOME/.local/pipx/venvs/codex-autorunner" "$HOME/.local/pipx/venvs/codex-autorunner.current"
    HUB_BIN="$HOME/.local/pipx/venvs/codex-autorunner.current/bin/codex-autorunner"
  fi
fi

mkdir -p "$(dirname "${LAUNCH_AGENT}")"
mkdir -p "${CAR_ROOT}/.codex-autorunner"

telegram_enabled() {
  if [[ "${ENABLE_TELEGRAM_BOT}" == "1" || "${ENABLE_TELEGRAM_BOT}" == "true" ]]; then
    return 0
  fi
  if [[ "${ENABLE_TELEGRAM_BOT}" == "0" || "${ENABLE_TELEGRAM_BOT}" == "false" ]]; then
    return 1
  fi
  local cfg
  cfg="${CAR_ROOT}/.codex-autorunner/config.yml"
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
  cfg="${CAR_ROOT}/.codex-autorunner/config.yml"
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${HUB_BIN} hub serve --host ${HUB_HOST} --port ${HUB_PORT} --base-path ${HUB_BASE_PATH} --path ${CAR_ROOT}</string>
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

if telegram_enabled; then
  echo "Writing LaunchAgent plist to ${TELEGRAM_LAUNCH_AGENT}..."
  cat > "${TELEGRAM_LAUNCH_AGENT}" <<EOF
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${HUB_BIN} telegram start --path ${CAR_ROOT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${CAR_ROOT}</string>
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

  echo "Reloading launchd service ${TELEGRAM_LABEL}..."
  launchctl unload -w "${TELEGRAM_LAUNCH_AGENT}" >/dev/null 2>&1 || true
  launchctl load -w "${TELEGRAM_LAUNCH_AGENT}"
  launchctl kickstart -k "gui/$(id -u)/${TELEGRAM_LABEL}"
fi

if discord_enabled; then
  echo "Writing LaunchAgent plist to ${DISCORD_LAUNCH_AGENT}..."
  cat > "${DISCORD_LAUNCH_AGENT}" <<EOF
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${HUB_BIN} discord start --path ${CAR_ROOT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${CAR_ROOT}</string>
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

  echo "Reloading launchd service ${DISCORD_LABEL}..."
  launchctl unload -w "${DISCORD_LAUNCH_AGENT}" >/dev/null 2>&1 || true
  launchctl load -w "${DISCORD_LAUNCH_AGENT}"
  launchctl kickstart -k "gui/$(id -u)/${DISCORD_LABEL}"
elif [[ -f "${DISCORD_LAUNCH_AGENT}" ]]; then
  echo "Discord disabled; unloading launchd service ${DISCORD_LABEL}..."
  launchctl unload -w "${DISCORD_LAUNCH_AGENT}" >/dev/null 2>&1 || true
fi

echo "Done."
