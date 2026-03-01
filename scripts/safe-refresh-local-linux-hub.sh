#!/usr/bin/env bash
set -euo pipefail

# Safe refresh for a systemd-user managed Linux hub.
#
# The update worker prepares source in PACKAGE_SRC, then this script:
# 1) installs the staged package into the current python environment
# 2) restarts selected systemd user services
# 3) verifies basic health checks
#
# Overrides:
#   PACKAGE_SRC                  Path to this repo (default: scripts/..)
#   UPDATE_STATUS_PATH           JSON status path maintained by update worker
#   UPDATE_TARGET                both|web|chat|telegram|discord (default: both)
#   UPDATE_HUB_SERVICE_NAME      systemd user service for hub (default: car-hub)
#   UPDATE_TELEGRAM_SERVICE_NAME systemd user service for telegram (default: car-telegram)
#   UPDATE_DISCORD_SERVICE_NAME  systemd user service for discord (default: car-discord)
#   HEALTH_URL                   web health URL (default: http://127.0.0.1:4173/health)
#   HEALTH_TIMEOUT_SECONDS       web health timeout (default: 30)
#   HEALTH_INTERVAL_SECONDS      web health poll interval (default: 0.5)
#   HELPER_PYTHON                python binary used for install/status writes
#   LOCAL_BIN                    User-local bin path to ensure in login shells (default: ~/.local/bin)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"
UPDATE_STATUS_PATH="${UPDATE_STATUS_PATH:-}"
UPDATE_TARGET="${UPDATE_TARGET:-both}"
UPDATE_HUB_SERVICE_NAME="${UPDATE_HUB_SERVICE_NAME:-car-hub}"
UPDATE_TELEGRAM_SERVICE_NAME="${UPDATE_TELEGRAM_SERVICE_NAME:-car-telegram}"
UPDATE_DISCORD_SERVICE_NAME="${UPDATE_DISCORD_SERVICE_NAME:-car-discord}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:4173/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-0.5}"
HELPER_PYTHON="${HELPER_PYTHON:-}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"

write_status() {
  local status message
  status="$1"
  message="$2"
  if [[ -z "${UPDATE_STATUS_PATH}" || -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
    return 0
  fi
  "${HELPER_PYTHON}" - <<PY
import json
import pathlib
import time

path = pathlib.Path("${UPDATE_STATUS_PATH}")
path.parent.mkdir(parents=True, exist_ok=True)
payload = {"status": "${status}", "message": "${message}", "at": time.time()}
try:
    existing = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    existing = None
if isinstance(existing, dict):
    for key in ("notify_chat_id", "notify_thread_id", "notify_reply_to", "notify_sent_at"):
        if key not in payload and key in existing:
            payload[key] = existing[key]
path.write_text(json.dumps(payload), encoding="utf-8")
PY
}

fail() {
  local message="$1"
  echo "${message}" >&2
  write_status "error" "${message}"
  exit 1
}

ensure_login_shell_path() {
  local path_entry marker_start marker_end
  path_entry="$1"
  marker_start="# >>> codex-autorunner local-bin >>>"
  marker_end="# <<< codex-autorunner local-bin <<<"
  if [[ -z "${HOME:-}" || ! -d "${HOME}" ]]; then
    echo "Skipping login-shell PATH bootstrap; HOME is unavailable." >&2
    return 0
  fi
  for profile in "${HOME}/.zprofile" "${HOME}/.bash_profile" "${HOME}/.profile"; do
    if ! mkdir -p "$(dirname "${profile}")"; then
      echo "Warning: could not create directory for ${profile}; skipping." >&2
      continue
    fi
    if [[ ! -e "${profile}" ]] && ! touch "${profile}"; then
      echo "Warning: could not create ${profile}; skipping." >&2
      continue
    fi
    if [[ ! -w "${profile}" ]]; then
      echo "Warning: ${profile} is not writable; skipping." >&2
      continue
    fi
    if grep -Fq "${marker_start}" "${profile}" 2>/dev/null; then
      continue
    fi
    if ! {
      echo ""
      echo "${marker_start}"
      echo "# Ensure user-local CAR binaries are available in login/non-interactive shells."
      printf 'export PATH="%s:$PATH"\n' "${path_entry}"
      echo "${marker_end}"
    } >> "${profile}"; then
      echo "Warning: failed to update ${profile}; skipping." >&2
      continue
    fi
    echo "Updated ${profile} to include ${path_entry} in PATH."
  done
}

normalize_update_target() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    ""|both|all)
      echo "both"
      ;;
    web|hub|server|ui)
      echo "web"
      ;;
    chat|chat-apps|apps)
      echo "chat"
      ;;
    telegram|tg|bot)
      echo "telegram"
      ;;
    discord|dc)
      echo "discord"
      ;;
    *)
      fail "Unsupported UPDATE_TARGET '${raw}'. Use both, web, chat, telegram, or discord."
      ;;
  esac
}

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python)"
  fi
fi

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  fail "Python not found (set HELPER_PYTHON)."
fi

for cmd in git systemctl curl; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    fail "Missing required command: ${cmd}."
  fi
done

if [[ ! -d "${PACKAGE_SRC}" ]]; then
  fail "PACKAGE_SRC does not exist: ${PACKAGE_SRC}"
fi

if [[ ! -f "${PACKAGE_SRC}/pyproject.toml" ]]; then
  fail "PACKAGE_SRC is missing pyproject.toml: ${PACKAGE_SRC}"
fi

target="$(normalize_update_target "${UPDATE_TARGET}")"

service_load_state() {
  local service_name="$1"
  systemctl --user show "${service_name}" --property LoadState --value 2>/dev/null || true
}

service_exists() {
  local service_name="$1"
  local state
  state="$(service_load_state "${service_name}")"
  [[ -n "${state}" && "${state}" != "not-found" ]]
}

wait_for_http_health() {
  local deadline now
  deadline="$("${HELPER_PYTHON}" - <<PY
import time
print(time.time() + float("${HEALTH_TIMEOUT_SECONDS}"))
PY
)"
  while true; do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi
    now="$("${HELPER_PYTHON}" - <<PY
import time
print(time.time())
PY
)"
    if ! "${HELPER_PYTHON}" - <<PY
now = float("${now}")
deadline = float("${deadline}")
raise SystemExit(0 if now <= deadline else 1)
PY
    then
      return 1
    fi
    sleep "${HEALTH_INTERVAL_SECONDS}"
  done
}

echo "Installing codex-autorunner from ${PACKAGE_SRC}..."
"${HELPER_PYTHON}" -m pip -q install --upgrade pip
"${HELPER_PYTHON}" -m pip -q install --upgrade "${PACKAGE_SRC}"
ensure_login_shell_path "${LOCAL_BIN}"

echo "Reloading systemd user manager..."
systemctl --user daemon-reload

restart_web=false
restart_telegram=false
restart_discord=false

if [[ "${target}" == "both" || "${target}" == "web" ]]; then
  if ! service_exists "${UPDATE_HUB_SERVICE_NAME}"; then
    fail "Hub service not found: ${UPDATE_HUB_SERVICE_NAME}"
  fi
  echo "Restarting hub service ${UPDATE_HUB_SERVICE_NAME}..."
  systemctl --user restart "${UPDATE_HUB_SERVICE_NAME}"
  restart_web=true
fi

if [[ "${target}" == "both" || "${target}" == "chat" || "${target}" == "telegram" ]]; then
  if service_exists "${UPDATE_TELEGRAM_SERVICE_NAME}"; then
    echo "Restarting telegram service ${UPDATE_TELEGRAM_SERVICE_NAME}..."
    systemctl --user restart "${UPDATE_TELEGRAM_SERVICE_NAME}"
    restart_telegram=true
  elif [[ "${target}" == "telegram" ]]; then
    fail "Telegram service not found: ${UPDATE_TELEGRAM_SERVICE_NAME}"
  else
    echo "Telegram service ${UPDATE_TELEGRAM_SERVICE_NAME} not found; skipping."
  fi
fi

if [[ "${target}" == "both" || "${target}" == "chat" || "${target}" == "discord" ]]; then
  if service_exists "${UPDATE_DISCORD_SERVICE_NAME}"; then
    echo "Restarting discord service ${UPDATE_DISCORD_SERVICE_NAME}..."
    systemctl --user restart "${UPDATE_DISCORD_SERVICE_NAME}"
    restart_discord=true
  elif [[ "${target}" == "discord" ]]; then
    fail "Discord service not found: ${UPDATE_DISCORD_SERVICE_NAME}"
  else
    echo "Discord service ${UPDATE_DISCORD_SERVICE_NAME} not found; skipping."
  fi
fi

if [[ "${restart_web}" == "true" ]]; then
  echo "Checking web health at ${HEALTH_URL}..."
  if ! wait_for_http_health; then
    fail "Web health check failed at ${HEALTH_URL}."
  fi
fi

if [[ "${restart_telegram}" == "true" ]]; then
  if ! systemctl --user is-active --quiet "${UPDATE_TELEGRAM_SERVICE_NAME}"; then
    fail "Telegram service is not active: ${UPDATE_TELEGRAM_SERVICE_NAME}"
  fi
fi

if [[ "${restart_discord}" == "true" ]]; then
  if ! systemctl --user is-active --quiet "${UPDATE_DISCORD_SERVICE_NAME}"; then
    fail "Discord service is not active: ${UPDATE_DISCORD_SERVICE_NAME}"
  fi
fi

message="Update completed successfully."
echo "${message}"
write_status "ok" "${message}"
