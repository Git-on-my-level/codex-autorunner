#!/usr/bin/env bash
set -euo pipefail

# Safe refresh for a launchd-managed local macOS hub.
#
# Strategy: install into a new venv, atomically flip a "current" symlink,
# restart launchd, and health-check. Roll back to "prev" on failure.
#
# Overrides:
#   PACKAGE_SRC            Path to this repo (default: scripts/..)
#   LABEL                  launchd label (default: com.codex.autorunner)
#   PLIST_PATH             launchd plist path (default: ~/Library/LaunchAgents/${LABEL}.plist)
#   ENABLE_TELEGRAM_BOT    Enable telegram bot LaunchAgent (auto|true|false; default: auto)
#   TELEGRAM_LABEL         launchd label for telegram bot (default: ${LABEL}.telegram)
#   TELEGRAM_PLIST_PATH    telegram plist path (default: ~/Library/LaunchAgents/${TELEGRAM_LABEL}.plist)
#   TELEGRAM_LOG           telegram stdout/stderr log path (default: <hub_root>/.codex-autorunner/codex-autorunner-telegram.log)
#   ENABLE_DISCORD_BOT     Enable discord bot LaunchAgent (auto|true|false; default: auto)
#   DISCORD_LABEL          launchd label for discord bot (default: ${LABEL}.discord)
#   DISCORD_PLIST_PATH     discord plist path (default: ~/Library/LaunchAgents/${DISCORD_LABEL}.plist)
#   DISCORD_LOG            discord stdout/stderr log path (default: <hub_root>/.codex-autorunner/codex-autorunner-discord.log)
#   UPDATE_TARGET          Which services to restart (both|web|chat|telegram|discord; default: both)
#   PIPX_ROOT              pipx root (default: ~/.local/pipx)
#   PIPX_VENV              existing pipx venv path (default: ${PIPX_ROOT}/venvs/codex-autorunner)
#   PIPX_PYTHON            python used for new venvs (default: pyenv python3, then Homebrew)
#   PYENV_PYTHON           override path used when pyenv is installed (optional)
#   CONFIG_PYTHON_KEY      config key for python selection (default: refresh.python)
#   CURRENT_VENV_LINK      symlink path used by launchd (default: ${PIPX_ROOT}/venvs/codex-autorunner.current)
#   PREV_VENV_LINK         symlink path used for rollback (default: ${PIPX_ROOT}/venvs/codex-autorunner.prev)
#   HEALTH_TIMEOUT_SECONDS seconds to wait for health (default: 30)
#   HEALTH_INTERVAL_SECONDS poll interval (default: 0.5)
#   HEALTH_PATH            request path (default: derived from base_path)
#   HEALTH_STATIC_PATH     static asset path (default: derived from base_path)
#   HEALTH_CHECK_STATIC    static asset check (auto|true|false; default: auto)
#   HEALTH_CHECK_TELEGRAM  telegram launchd check (auto|true|false; default: auto)
#   HEALTH_CHECK_DISCORD   discord launchd check (auto|true|false; default: auto)
#   HEALTH_CONNECT_TIMEOUT_SECONDS connection timeout for each health request (default: 2)
#   HEALTH_REQUEST_TIMEOUT_SECONDS total timeout for each health request (default: 5)
#   KEEP_OLD_VENVS         how many old next-* venvs to keep (default: 3)
#   NVM_BIN                Node bin path to prepend (default: ~/.nvm/versions/node/v22.12.0/bin)
#   LOCAL_BIN              Local bin path to prepend (default: ~/.local/bin)
#   PY39_BIN               Python bin path to prepend (default: ~/Library/Python/3.9/bin)
#   OPENCODE_BIN           OpenCode bin path to prepend (default: ~/.opencode/bin)

LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
UPDATE_STATUS_PATH="${UPDATE_STATUS_PATH:-}"
TELEGRAM_LABEL="${TELEGRAM_LABEL:-${LABEL}.telegram}"
TELEGRAM_PLIST_PATH="${TELEGRAM_PLIST_PATH:-$HOME/Library/LaunchAgents/${TELEGRAM_LABEL}.plist}"
ENABLE_TELEGRAM_BOT="${ENABLE_TELEGRAM_BOT:-auto}"
DISCORD_LABEL="${DISCORD_LABEL:-${LABEL}.discord}"
DISCORD_PLIST_PATH="${DISCORD_PLIST_PATH:-$HOME/Library/LaunchAgents/${DISCORD_LABEL}.plist}"
ENABLE_DISCORD_BOT="${ENABLE_DISCORD_BOT:-auto}"
UPDATE_TARGET="${UPDATE_TARGET:-both}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

PIPX_ROOT="${PIPX_ROOT:-$HOME/.local/pipx}"
PIPX_VENV="${PIPX_VENV:-$PIPX_ROOT/venvs/codex-autorunner}"
PIPX_PYTHON="${PIPX_PYTHON:-}"
CURRENT_VENV_LINK="${CURRENT_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.current}"
PREV_VENV_LINK="${PREV_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.prev}"
HELPER_PYTHON="${HELPER_PYTHON:-$PIPX_PYTHON}"
CONFIG_PYTHON_KEY="${CONFIG_PYTHON_KEY:-refresh.python}"

HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-0.5}"
HEALTH_CONNECT_TIMEOUT_SECONDS="${HEALTH_CONNECT_TIMEOUT_SECONDS:-2}"
HEALTH_REQUEST_TIMEOUT_SECONDS="${HEALTH_REQUEST_TIMEOUT_SECONDS:-5}"
HEALTH_PATH="${HEALTH_PATH:-}"
HEALTH_STATIC_PATH="${HEALTH_STATIC_PATH:-}"
HEALTH_CHECK_STATIC="${HEALTH_CHECK_STATIC:-auto}"
HEALTH_CHECK_TELEGRAM="${HEALTH_CHECK_TELEGRAM:-auto}"
HEALTH_CHECK_DISCORD="${HEALTH_CHECK_DISCORD:-auto}"
KEEP_OLD_VENVS="${KEEP_OLD_VENVS:-3}"
NVM_BIN="${NVM_BIN:-$HOME/.nvm/versions/node/v22.12.0/bin}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PY39_BIN="${PY39_BIN:-$HOME/Library/Python/3.9/bin}"
OPENCODE_BIN="${OPENCODE_BIN:-$HOME/.opencode/bin}"

current_target=""
swap_completed=false
rollback_completed=false

write_status() {
  local status message
  status="$1"
  message="$2"
  if [[ -z "${UPDATE_STATUS_PATH}" || ! -x "${HELPER_PYTHON}" ]]; then
    return 0
  fi
  "${HELPER_PYTHON}" - <<PY
import json, pathlib, time
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
      echo "# Ensure pipx-installed CAR is available in login/non-interactive shells."
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

normalize_bool() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    1|true|yes|y|on)
      echo "true"
      ;;
    0|false|no|n|off)
      echo "false"
      ;;
    ""|auto)
      echo "auto"
      ;;
    *)
      echo "auto"
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

if [[ ! -x "${HELPER_PYTHON}" ]]; then
  fail "Python not found (set PIPX_PYTHON or HELPER_PYTHON)."
fi

_plist_arg_value() {
  local key
  key="$1"
  "${HELPER_PYTHON}" - "$key" "${PLIST_PATH}" <<'PY'
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

_config_python() {
  local root key
  root="$1"
  key="$2"
  "${HELPER_PYTHON}" - "$root" "$key" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    sys.exit(0)

root = Path(sys.argv[1]).expanduser()
key = sys.argv[2]
config_path = root / ".codex-autorunner" / "config.yml"
if not config_path.exists():
    sys.exit(0)

try:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
except Exception:
    sys.exit(0)

if not isinstance(data, dict) or not key:
    sys.exit(0)

value = data
for part in key.split("."):
    if not isinstance(value, dict):
        value = None
        break
    value = value.get(part)
if isinstance(value, str) and value.strip():
    sys.stdout.write(value.strip())
PY
}

_resolve_pyenv_python() {
  local candidate
  if command -v pyenv >/dev/null 2>&1; then
    candidate="$(pyenv which python3 2>/dev/null || true)"
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
    candidate="$(pyenv which python 2>/dev/null || true)"
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  fi
  return 1
}

_resolve_config_python() {
  local hub_root raw version candidate
  hub_root="$(_plist_arg_value path)"
  if [[ -z "${hub_root}" ]]; then
    return 1
  fi
  raw="$(_config_python "${hub_root}" "${CONFIG_PYTHON_KEY}")"
  if [[ -z "${raw}" ]]; then
    return 1
  fi
  case "${raw}" in
    pyenv)
      _resolve_pyenv_python
      return $?
      ;;
    pyenv:*)
      version="${raw#pyenv:}"
      if command -v pyenv >/dev/null 2>&1; then
        candidate="$(PYENV_VERSION="${version}" pyenv which python3 2>/dev/null || true)"
        if [[ -x "${candidate}" ]]; then
          echo "${candidate}"
          return 0
        fi
        candidate="$(PYENV_VERSION="${version}" pyenv which python 2>/dev/null || true)"
        if [[ -x "${candidate}" ]]; then
          echo "${candidate}"
          return 0
        fi
      fi
      return 1
      ;;
  esac
  if [[ -x "${raw}" ]]; then
    echo "${raw}"
    return 0
  fi
  return 1
}

if [[ -z "${PIPX_PYTHON}" || ! -x "${PIPX_PYTHON}" ]]; then
  PIPX_PYTHON="$(_resolve_config_python || true)"
fi

if [[ -z "${PIPX_PYTHON}" || ! -x "${PIPX_PYTHON}" ]]; then
  if [[ -n "${PYENV_PYTHON:-}" && -x "${PYENV_PYTHON}" ]]; then
    PIPX_PYTHON="${PYENV_PYTHON}"
  fi
fi

if [[ -z "${PIPX_PYTHON}" || ! -x "${PIPX_PYTHON}" ]]; then
  PIPX_PYTHON="$(_resolve_pyenv_python || true)"
fi

if [[ -z "${PIPX_PYTHON}" || ! -x "${PIPX_PYTHON}" ]]; then
  if [[ -x "/opt/homebrew/bin/python3" ]]; then
    PIPX_PYTHON="/opt/homebrew/bin/python3"
  elif [[ -x "${PIPX_VENV}/bin/python" ]]; then
    PIPX_PYTHON="${PIPX_VENV}/bin/python"
  fi
fi

if [[ -z "${PIPX_PYTHON}" || ! -x "${PIPX_PYTHON}" ]]; then
  fail "Unable to resolve a Python interpreter for pipx venv creation."
fi

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  HELPER_PYTHON="${PIPX_PYTHON}"
fi

UPDATE_TARGET="$(normalize_update_target "${UPDATE_TARGET}")"
HEALTH_CHECK_STATIC="$(normalize_bool "${HEALTH_CHECK_STATIC}")"
HEALTH_CHECK_TELEGRAM="$(normalize_bool "${HEALTH_CHECK_TELEGRAM}")"
HEALTH_CHECK_DISCORD="$(normalize_bool "${HEALTH_CHECK_DISCORD}")"
should_reload_hub=false
should_reload_telegram=false
should_reload_discord=false
telegram_health_reason=""
telegram_health_checked=false
discord_health_reason=""
discord_health_checked=false
case "${UPDATE_TARGET}" in
  both)
    should_reload_hub=true
    should_reload_telegram=true
    should_reload_discord=true
    ;;
  web)
    should_reload_hub=true
    ;;
  chat)
    should_reload_telegram=true
    should_reload_discord=true
    ;;
  telegram)
    should_reload_telegram=true
    ;;
  discord)
    should_reload_discord=true
    ;;
esac

if [[ ! -f "${PLIST_PATH}" ]]; then
  fail "LaunchAgent plist not found at ${PLIST_PATH}. Run scripts/install-local-mac-hub.sh or scripts/launchd-hub.sh (or set PLIST_PATH)."
fi

_realpath() {
  "${HELPER_PYTHON}" - "$1" <<'PY'
import os
import sys

try:
    print(os.path.realpath(sys.argv[1]))
except Exception:
    pass
PY
}

if [[ ! -d "${PIPX_VENV}" ]]; then
  if [[ -L "${CURRENT_VENV_LINK}" ]]; then
    current_target="$(_realpath "${CURRENT_VENV_LINK}")"
    if [[ -n "${current_target}" && -d "${current_target}" ]]; then
      echo "PIPX_VENV not found; using ${current_target} as current venv."
      PIPX_VENV="${current_target}"
    else
      fail "Expected pipx venv not found at ${PIPX_VENV}."
    fi
  else
    fail "Expected pipx venv not found at ${PIPX_VENV}."
  fi
fi

for cmd in git launchctl curl pipx; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    fail "Missing required command: ${cmd}."
  fi
done

if [[ ! -L "${CURRENT_VENV_LINK}" ]]; then
  echo "Initializing ${CURRENT_VENV_LINK} -> ${PIPX_VENV}"
  ln -sfn "${PIPX_VENV}" "${CURRENT_VENV_LINK}"
fi

current_target="$(_realpath "${CURRENT_VENV_LINK}")"
if [[ -z "${current_target}" ]]; then
  fail "Unable to resolve current venv from ${CURRENT_VENV_LINK}."
fi

ts="$(date +%Y%m%d-%H%M%S)"
next_venv="${PIPX_ROOT}/venvs/codex-autorunner.next-${ts}"

echo "Creating staged venv at ${next_venv} (python: ${PIPX_PYTHON})..."
"${PIPX_PYTHON}" -m venv "${next_venv}"
"${next_venv}/bin/python" -m pip -q install --upgrade pip

echo "Installing codex-autorunner from ${PACKAGE_SRC} into staged venv..."
"${next_venv}/bin/python" -m pip -q install --force-reinstall "${PACKAGE_SRC}"

echo "Smoke-checking staged venv imports..."
"${next_venv}/bin/python" -c "import codex_autorunner; from codex_autorunner.server import create_hub_app; print('ok')"
echo "Smoke-checking hub startup lifecycle..."
"${next_venv}/bin/python" - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.server import create_hub_app

with TemporaryDirectory() as tmp:
    hub_root = Path(tmp)
    seed_hub_files(hub_root, force=True)
    app = create_hub_app(hub_root)
    with TestClient(app) as client:
        for path in ("/health", "/car/health"):
            response = client.get(path)
            if response.status_code == 200:
                break
        else:
            raise SystemExit("hub startup smoke failed: /health endpoint unavailable")
print("hub startup ok")
PY
echo "Smoke-checking telegram module..."
"${next_venv}/bin/python" - <<'PY'
import importlib.util
import py_compile

spec = importlib.util.find_spec("codex_autorunner.integrations.telegram.service")
if spec is None or spec.origin is None:
    raise SystemExit("telegram service module not found in staged venv")
py_compile.compile(spec.origin, doraise=True)
print("telegram service ok")
PY

domain="gui/$(id -u)/${LABEL}"

_require_gui_domain() {
  local gui_domain
  gui_domain="gui/$(id -u)"
  if ! launchctl print "${gui_domain}" >/dev/null 2>&1; then
    fail "No active GUI launchd session (${gui_domain}); please log into the macOS GUI or bootstrap the LaunchAgents via 'sudo launchctl bootstrap ${gui_domain} ~/Library/LaunchAgents/${LABEL}.plist' before running this refresh."
  fi
}

_ensure_plist_uses_current_venv() {
  local desired_bin
  desired_bin="${CURRENT_VENV_LINK}/bin/codex-autorunner"

  if grep -q "${desired_bin}" "${PLIST_PATH}"; then
    return 0
  fi

  echo "Updating plist to use ${desired_bin}..."
  "${HELPER_PYTHON}" - <<PY
from __future__ import annotations

from pathlib import Path

plist_path = Path("${PLIST_PATH}")
desired = "${desired_bin}"

text = plist_path.read_text()
replacements = [
    "; codex-autorunner hub serve",
    " codex-autorunner hub serve",
    ">codex-autorunner hub serve",
    "codex-autorunner hub serve",
]

new_text = text
for needle in replacements:
    if needle in new_text:
        new_text = new_text.replace(needle, needle.replace("codex-autorunner", desired), 1)
        break

if new_text == text:
    raise SystemExit(
        "Unable to update plist automatically; expected to find a 'codex-autorunner hub serve' command."
    )

plist_path.write_text(new_text)
PY
}

_ensure_plist_has_opencode_path() {
  if [[ ! -f "${PLIST_PATH}" ]]; then
    return 0
  fi

  "${HELPER_PYTHON}" - <<PY
from __future__ import annotations

import plistlib
from pathlib import Path

plist_path = Path("${PLIST_PATH}")
opencode_bin = "${OPENCODE_BIN}"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

program_args = plist.get("ProgramArguments")
if not isinstance(program_args, list) or len(program_args) < 3:
    raise SystemExit("LaunchAgent plist missing ProgramArguments list.")

cmd = program_args[2]
if not isinstance(cmd, str):
    raise SystemExit("LaunchAgent plist ProgramArguments[2] is not a string.")

updated = False

if opencode_bin and opencode_bin not in cmd:
    if "PATH=" in cmd:
        cmd = cmd.replace("PATH=", f"PATH={opencode_bin}:", 1)
    else:
        cmd = f"PATH={opencode_bin}:$PATH; {cmd}"
    updated = True

if updated:
    program_args[2] = cmd
    plist["ProgramArguments"] = program_args
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
PY
}

_normalize_plist_process_limits() {
  local plist_path
  plist_path="$1"
  if [[ -z "${plist_path}" || ! -f "${plist_path}" ]]; then
    return 0
  fi

  "${HELPER_PYTHON}" - "$plist_path" <<'PY'
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

updated = False
for key in ("SoftResourceLimits", "HardResourceLimits"):
    section = plist.get(key)
    if not isinstance(section, dict):
        continue
    if "NumberOfProcesses" in section:
        section.pop("NumberOfProcesses", None)
        updated = True
    if not section:
        plist.pop(key, None)
        updated = True
    else:
        plist[key] = section

if updated:
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
PY
}

_service_pid() {
  launchctl print "${domain}" 2>/dev/null | awk '/pid =/ {print $3; exit}'
}

_telegram_service_pid() {
  local telegram_domain
  telegram_domain="gui/$(id -u)/${TELEGRAM_LABEL}"
  launchctl print "${telegram_domain}" 2>/dev/null | awk '/pid =/ {print $3; exit}'
}

_discord_service_pid() {
  local discord_domain
  discord_domain="gui/$(id -u)/${DISCORD_LABEL}"
  launchctl print "${discord_domain}" 2>/dev/null | awk '/pid =/ {print $3; exit}'
}

_wait_pid_exit() {
  local pid start
  pid="$1"
  start="$(date +%s)"
  while kill -0 "${pid}" >/dev/null 2>&1; do
    if (( $(date +%s) - start >= 5 )); then
      return 1
    fi
    sleep 0.1
  done
  return 0
}

_reload() {
  local pid
  _require_gui_domain
  pid="$(_service_pid)"
  _ensure_plist_has_opencode_path
  _normalize_plist_process_limits "${PLIST_PATH}"
  launchctl unload -w "${PLIST_PATH}" >/dev/null 2>&1 || true
  if [[ -n "${pid}" && "${pid}" != "0" ]]; then
    if ! _wait_pid_exit "${pid}"; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
  fi
  launchctl load -w "${PLIST_PATH}" >/dev/null
  launchctl kickstart -k "${domain}" >/dev/null
}

_reload_telegram() {
  local hub_root telegram_state telegram_domain
  _require_gui_domain
  hub_root="$(_plist_arg_value path)"
  telegram_state="$(_telegram_state "${hub_root}")"

  if [[ "${telegram_state}" == "enabled" ]]; then
    if [[ -z "${hub_root}" ]]; then
      echo "Telegram enabled but unable to derive hub root; skipping telegram LaunchAgent." >&2
      return 0
    fi
    if [[ ! -f "${TELEGRAM_PLIST_PATH}" ]]; then
      _write_telegram_plist "${hub_root}"
    fi
    _ensure_telegram_plist_uses_current_venv
    PLIST_PATH="${TELEGRAM_PLIST_PATH}" _ensure_plist_has_opencode_path
    _normalize_plist_process_limits "${TELEGRAM_PLIST_PATH}"
    telegram_domain="gui/$(id -u)/${TELEGRAM_LABEL}"
    launchctl unload -w "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
    launchctl load -w "${TELEGRAM_PLIST_PATH}" >/dev/null
    launchctl kickstart -k "${telegram_domain}" >/dev/null
    return 0
  fi

  if [[ "${telegram_state}" == "disabled" ]]; then
    if [[ -f "${TELEGRAM_PLIST_PATH}" ]]; then
      echo "Telegram disabled; unloading launchd service ${TELEGRAM_LABEL}..."
      launchctl unload -w "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
    fi
    return 0
  fi

  if [[ ! -f "${TELEGRAM_PLIST_PATH}" ]]; then
    return 0
  fi
  _normalize_plist_process_limits "${TELEGRAM_PLIST_PATH}"
  telegram_domain="gui/$(id -u)/${TELEGRAM_LABEL}"
  launchctl unload -w "${TELEGRAM_PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${TELEGRAM_PLIST_PATH}" >/dev/null
  launchctl kickstart -k "${telegram_domain}" >/dev/null
}

_reload_discord() {
  local hub_root discord_state discord_domain missing_envs
  _require_gui_domain
  hub_root="$(_plist_arg_value path)"
  discord_state="$(_discord_state "${hub_root}")"

  if [[ "${discord_state}" == "enabled" ]]; then
    if [[ -z "${hub_root}" ]]; then
      echo "Discord enabled but unable to derive hub root; skipping discord LaunchAgent." >&2
      return 0
    fi
    if [[ ! -f "${DISCORD_PLIST_PATH}" ]]; then
      _write_discord_plist "${hub_root}"
    fi
    _ensure_discord_plist_uses_current_venv
    PLIST_PATH="${DISCORD_PLIST_PATH}" _ensure_plist_has_opencode_path
    _normalize_plist_process_limits "${DISCORD_PLIST_PATH}"
    discord_domain="gui/$(id -u)/${DISCORD_LABEL}"
    launchctl unload -w "${DISCORD_PLIST_PATH}" >/dev/null 2>&1 || true
    launchctl load -w "${DISCORD_PLIST_PATH}" >/dev/null
    launchctl kickstart -k "${discord_domain}" >/dev/null
    return 0
  fi

  if [[ "${discord_state}" == "disabled" || "${discord_state}" == "missing_env" ]]; then
    if [[ -f "${DISCORD_PLIST_PATH}" ]]; then
      if [[ "${discord_state}" == "missing_env" ]]; then
        missing_envs="$(_discord_missing_env_names "${hub_root}")"
        if [[ -n "${missing_envs}" ]]; then
          echo "Discord enabled but missing env vars (${missing_envs}); unloading launchd service ${DISCORD_LABEL}..." >&2
        else
          echo "Discord enabled but missing required env vars; unloading launchd service ${DISCORD_LABEL}..." >&2
        fi
      else
        echo "Discord disabled; unloading launchd service ${DISCORD_LABEL}..."
      fi
      launchctl unload -w "${DISCORD_PLIST_PATH}" >/dev/null 2>&1 || true
    fi
    return 0
  fi

  if [[ ! -f "${DISCORD_PLIST_PATH}" ]]; then
    return 0
  fi
  _normalize_plist_process_limits "${DISCORD_PLIST_PATH}"
  discord_domain="gui/$(id -u)/${DISCORD_LABEL}"
  launchctl unload -w "${DISCORD_PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${DISCORD_PLIST_PATH}" >/dev/null
  launchctl kickstart -k "${discord_domain}" >/dev/null
}

_telegram_state() {
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

_env_var_is_set() {
  local root name
  root="$1"
  name="$2"
  if [[ ! "${name}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    return 1
  fi
  if [[ -n "${!name:-}" ]]; then
    return 0
  fi
  if [[ -z "${root}" ]]; then
    return 1
  fi
  "${HELPER_PYTHON}" - "$root" "$name" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser()
key = sys.argv[2]

if not key:
    raise SystemExit(1)

try:
    from dotenv import dotenv_values  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    dotenv_values = None


def parse_fallback(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            if "=" not in stripped:
                continue
            k, value = stripped.split("=", 1)
            k = k.strip()
            if not k:
                continue
            value = value.strip()
            if value and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            env[k] = value
    except OSError:
        return {}
    return env


found: str | None = None
for candidate in (root / ".env", root / ".codex-autorunner" / ".env"):
    if not candidate.exists():
        continue
    if dotenv_values is not None:
        values = dotenv_values(candidate)
        value = values.get(key) if isinstance(values, dict) else None
        if value is not None:
            found = str(value)
        continue
    fallback = parse_fallback(candidate)
    if key in fallback:
        found = fallback[key]

if found:
    raise SystemExit(0)
raise SystemExit(1)
PY
}

_discord_config() {
  local root cfg
  root="$1"
  if [[ -z "${root}" ]]; then
    echo "unknown CAR_DISCORD_BOT_TOKEN CAR_DISCORD_APP_ID"
    return 0
  fi
  cfg="${root}/.codex-autorunner/config.yml"
  if [[ ! -f "${cfg}" ]]; then
    echo "unknown CAR_DISCORD_BOT_TOKEN CAR_DISCORD_APP_ID"
    return 0
  fi
  "${HELPER_PYTHON}" - "$cfg" <<'PY'
import sys
from pathlib import Path

DEFAULT_BOT = "CAR_DISCORD_BOT_TOKEN"
DEFAULT_APP = "CAR_DISCORD_APP_ID"

cfg = Path(sys.argv[1])
try:
    import yaml
except Exception:
    print(f"unknown {DEFAULT_BOT} {DEFAULT_APP}")
    raise SystemExit(0)

try:
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
except Exception:
    print(f"unknown {DEFAULT_BOT} {DEFAULT_APP}")
    raise SystemExit(0)

if not isinstance(data, dict):
    print(f"unknown {DEFAULT_BOT} {DEFAULT_APP}")
    raise SystemExit(0)

discord = data.get("discord_bot")
if not isinstance(discord, dict):
    print(f"disabled {DEFAULT_BOT} {DEFAULT_APP}")
    raise SystemExit(0)

enabled = bool(discord.get("enabled", False))
bot_env = str(discord.get("bot_token_env", DEFAULT_BOT)).strip() or DEFAULT_BOT
app_env = str(discord.get("app_id_env", DEFAULT_APP)).strip() or DEFAULT_APP
state = "enabled" if enabled else "disabled"
print(f"{state} {bot_env} {app_env}")
PY
}

_discord_missing_env_names() {
  local root cfg_state bot_env app_env missing
  root="$1"
  read -r cfg_state bot_env app_env <<<"$(_discord_config "${root}")"
  missing=()
  if ! _env_var_is_set "${root}" "${bot_env}"; then
    missing+=( "${bot_env}" )
  fi
  if ! _env_var_is_set "${root}" "${app_env}"; then
    missing+=( "${app_env}" )
  fi
  printf '%s\n' "${missing[*]:-}"
}

_discord_state() {
  local root cfg_state bot_env app_env
  root="$1"
  read -r cfg_state bot_env app_env <<<"$(_discord_config "${root}")"

  if [[ "${ENABLE_DISCORD_BOT}" == "0" || "${ENABLE_DISCORD_BOT}" == "false" ]]; then
    echo "disabled"
    return 0
  fi
  if [[ "${ENABLE_DISCORD_BOT}" == "1" || "${ENABLE_DISCORD_BOT}" == "true" ]]; then
    cfg_state="enabled"
    if [[ -z "${bot_env}" ]]; then
      bot_env="CAR_DISCORD_BOT_TOKEN"
    fi
    if [[ -z "${app_env}" ]]; then
      app_env="CAR_DISCORD_APP_ID"
    fi
  fi

  if [[ "${cfg_state}" != "enabled" ]]; then
    echo "${cfg_state}"
    return 0
  fi

  if ! _env_var_is_set "${root}" "${bot_env}" || ! _env_var_is_set "${root}" "${app_env}"; then
    echo "missing_env"
    return 0
  fi

  echo "enabled"
}

_ensure_telegram_plist_uses_current_venv() {
  local desired_bin
  desired_bin="${CURRENT_VENV_LINK}/bin/codex-autorunner"

  if [[ ! -f "${TELEGRAM_PLIST_PATH}" ]]; then
    return 0
  fi

  if grep -q "${desired_bin}" "${TELEGRAM_PLIST_PATH}"; then
    return 0
  fi

  echo "Updating telegram plist to use ${desired_bin}..."
  "${HELPER_PYTHON}" - <<PY
from __future__ import annotations

import plistlib
import re
from pathlib import Path

plist_path = Path("${TELEGRAM_PLIST_PATH}")
desired = "${desired_bin}"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

program_args = plist.get("ProgramArguments")
if not isinstance(program_args, list):
    raise SystemExit("Telegram plist missing ProgramArguments list.")

pattern = re.compile(r"(^|[\\s;])[^\\s;]*codex-autorunner(?= telegram start\\b)")
updated = False
for idx, arg in enumerate(program_args):
    if not isinstance(arg, str):
        continue
    if "telegram start" not in arg or "codex-autorunner" not in arg:
        continue
    new_arg, count = pattern.subn(lambda m: f"{m.group(1)}{desired}", arg, count=1)
    if count == 0 and "codex-autorunner telegram start" in arg:
        new_arg = arg.replace("codex-autorunner telegram start", f"{desired} telegram start", 1)
        count = 1
    if count:
        program_args[idx] = new_arg
        updated = True
    break

if not updated:
    raise SystemExit(
        "Unable to update telegram plist automatically; expected to find a 'codex-autorunner telegram start' command."
    )

with plist_path.open("wb") as handle:
    plistlib.dump(plist, handle)
PY
}

_write_telegram_plist() {
  local root telegram_log
  root="$1"
  telegram_log="${TELEGRAM_LOG:-${root}/.codex-autorunner/codex-autorunner-telegram.log}"
  echo "Writing launchd plist to ${TELEGRAM_PLIST_PATH}..."
  mkdir -p "$(dirname "${TELEGRAM_PLIST_PATH}")"
  mkdir -p "${root}/.codex-autorunner"
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${CURRENT_VENV_LINK}/bin/codex-autorunner telegram start --path ${root}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${root}</string>
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
}

_ensure_discord_plist_uses_current_venv() {
  local desired_bin
  desired_bin="${CURRENT_VENV_LINK}/bin/codex-autorunner"

  if [[ ! -f "${DISCORD_PLIST_PATH}" ]]; then
    return 0
  fi

  if grep -q "${desired_bin}" "${DISCORD_PLIST_PATH}"; then
    return 0
  fi

  echo "Updating discord plist to use ${desired_bin}..."
  "${HELPER_PYTHON}" - <<PY
from __future__ import annotations

import plistlib
import re
from pathlib import Path

plist_path = Path("${DISCORD_PLIST_PATH}")
desired = "${desired_bin}"

with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)

program_args = plist.get("ProgramArguments")
if not isinstance(program_args, list):
    raise SystemExit("Discord plist missing ProgramArguments list.")

pattern = re.compile(r"(^|[\\s;])[^\\s;]*codex-autorunner(?= discord start\\b)")
updated = False
for idx, arg in enumerate(program_args):
    if not isinstance(arg, str):
        continue
    if "discord start" not in arg or "codex-autorunner" not in arg:
        continue
    new_arg, count = pattern.subn(lambda m: f"{m.group(1)}{desired}", arg, count=1)
    if count == 0 and "codex-autorunner discord start" in arg:
        new_arg = arg.replace("codex-autorunner discord start", f"{desired} discord start", 1)
        count = 1
    if count:
        program_args[idx] = new_arg
        updated = True
    break

if not updated:
    raise SystemExit(
        "Unable to update discord plist automatically; expected to find a 'codex-autorunner discord start' command."
    )

with plist_path.open("wb") as handle:
    plistlib.dump(plist, handle)
PY
}

_write_discord_plist() {
  local root discord_log
  root="$1"
  discord_log="${DISCORD_LOG:-${root}/.codex-autorunner/codex-autorunner-discord.log}"
  echo "Writing launchd plist to ${DISCORD_PLIST_PATH}..."
  mkdir -p "$(dirname "${DISCORD_PLIST_PATH}")"
  mkdir -p "${root}/.codex-autorunner"
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
    <string>PATH=${OPENCODE_BIN}:${NVM_BIN}:${LOCAL_BIN}:${PY39_BIN}:\$PATH; ${CURRENT_VENV_LINK}/bin/codex-autorunner discord start --path ${root}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${root}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${discord_log}</string>
  <key>StandardErrorPath</key>
  <string>${discord_log}</string>
</dict>
</plist>
EOF
}

_normalize_base_path() {
  local base
  base="$1"
  if [[ -z "${base}" ]]; then
    echo ""
    return
  fi
  if [[ "${base:0:1}" != "/" ]]; then
    base="/${base}"
  fi
  base="${base%/}"
  if [[ "${base}" == "/" ]]; then
    base=""
  fi
  echo "${base}"
}

_config_base_path() {
  local root
  root="$1"
  "${HELPER_PYTHON}" - "$root" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    sys.exit(0)

root = Path(sys.argv[1]).expanduser()
config_path = root / ".codex-autorunner" / "config.yml"
if not config_path.exists():
    sys.exit(0)

try:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
except Exception:
    sys.exit(0)

if not isinstance(data, dict):
    sys.exit(0)

server = data.get("server")
if isinstance(server, dict):
    base_path = server.get("base_path")
    if isinstance(base_path, str) and base_path.strip():
        sys.stdout.write(base_path.strip())
PY
}

_config_server_port() {
  local root
  root="$1"
  "${HELPER_PYTHON}" - "$root" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    sys.exit(0)

root = Path(sys.argv[1]).expanduser()
config_path = root / ".codex-autorunner" / "config.yml"
if not config_path.exists():
    sys.exit(0)

try:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
except Exception:
    sys.exit(0)

if not isinstance(data, dict):
    sys.exit(0)

server = data.get("server")
if isinstance(server, dict):
    port = server.get("port")
    if isinstance(port, int) and port > 0:
        sys.stdout.write(str(port))
    elif isinstance(port, str) and port.strip().isdigit():
        sys.stdout.write(port.strip())
PY
}

_detect_base_path() {
  local base hub_root
  base="$(_plist_arg_value base-path)"
  if [[ -n "${base}" ]]; then
    _normalize_base_path "${base}"
    return
  fi
  hub_root="$(_plist_arg_value path)"
  if [[ -z "${hub_root}" ]]; then
    echo ""
    return
  fi
  base="$(_config_base_path "${hub_root}")"
  _normalize_base_path "${base}"
}

if [[ -z "${HEALTH_PATH}" ]]; then
  base_path="$(_detect_base_path)"
  if [[ -n "${base_path}" ]]; then
    HEALTH_PATH="${base_path}/health"
    if [[ -z "${HEALTH_STATIC_PATH}" ]]; then
      HEALTH_STATIC_PATH="${base_path}/static/app.js"
    fi
  else
    HEALTH_PATH="/health"
    if [[ -z "${HEALTH_STATIC_PATH}" ]]; then
      HEALTH_STATIC_PATH="/static/app.js"
    fi
  fi
fi

if [[ "${HEALTH_PATH:0:1}" != "/" ]]; then
  HEALTH_PATH="/${HEALTH_PATH}"
fi
if [[ -n "${HEALTH_STATIC_PATH}" && "${HEALTH_STATIC_PATH:0:1}" != "/" ]]; then
  HEALTH_STATIC_PATH="/${HEALTH_STATIC_PATH}"
fi

_should_check_static() {
  if [[ "${HEALTH_CHECK_STATIC}" == "false" ]]; then
    return 1
  fi
  if [[ "${HEALTH_CHECK_STATIC}" == "true" ]]; then
    return 0
  fi
  [[ -n "${HEALTH_STATIC_PATH}" ]]
}

_should_check_telegram() {
  local hub_root telegram_state
  if [[ "${HEALTH_CHECK_TELEGRAM}" == "false" ]]; then
    return 1
  fi
  if [[ "${HEALTH_CHECK_TELEGRAM}" == "true" ]]; then
    return 0
  fi
  if [[ ! -f "${TELEGRAM_PLIST_PATH}" ]]; then
    return 1
  fi
  hub_root="$(_plist_arg_value path)"
  telegram_state="$(_telegram_state "${hub_root}")"
  [[ "${telegram_state}" != "disabled" ]]
}

_should_check_discord() {
  local hub_root discord_state
  if [[ "${HEALTH_CHECK_DISCORD}" == "false" ]]; then
    return 1
  fi
  if [[ "${HEALTH_CHECK_DISCORD}" == "true" ]]; then
    return 0
  fi
  if [[ ! -f "${DISCORD_PLIST_PATH}" ]]; then
    return 1
  fi
  hub_root="$(_plist_arg_value path)"
  discord_state="$(_discord_state "${hub_root}")"
  [[ "${discord_state}" != "disabled" && "${discord_state}" != "missing_env" ]]
}

_health_check_once() {
  local port url static_url hub_root
  port="$(_plist_arg_value port)"
  if [[ -z "${port}" ]]; then
    hub_root="$(_plist_arg_value path)"
    if [[ -n "${hub_root}" ]]; then
      port="$(_config_server_port "${hub_root}")"
    fi
  fi
  if [[ -z "${port}" ]]; then
    port="4173"
  fi
  # Always use loopback; hub may bind 0.0.0.0. HEALTH_PATH is absolute.
  url="http://127.0.0.1:${port}${HEALTH_PATH}"
  curl -fsS --connect-timeout "${HEALTH_CONNECT_TIMEOUT_SECONDS}" \
    --max-time "${HEALTH_REQUEST_TIMEOUT_SECONDS}" \
    "${url}" >/dev/null 2>&1
  if _should_check_static; then
    static_url="http://127.0.0.1:${port}${HEALTH_STATIC_PATH}"
    curl -fsS --connect-timeout "${HEALTH_CONNECT_TIMEOUT_SECONDS}" \
      --max-time "${HEALTH_REQUEST_TIMEOUT_SECONDS}" \
      "${static_url}" >/dev/null 2>&1
  fi
}

_wait_healthy() {
  local start now
  start="$(date +%s)"
  while true; do
    if _health_check_once; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= HEALTH_TIMEOUT_SECONDS )); then
      return 1
    fi
    sleep "${HEALTH_INTERVAL_SECONDS}"
  done
}

_telegram_check_once() {
  local hub_root telegram_cmd
  hub_root="$(_plist_arg_value path)"
  if [[ -z "${hub_root}" ]]; then
    return 1
  fi
  telegram_cmd="${CURRENT_VENV_LINK}/bin/codex-autorunner"
  if [[ ! -x "${telegram_cmd}" ]]; then
    return 1
  fi
  # First ensure the state DB schema can be opened/migrated; then ping Telegram API.
  "${telegram_cmd}" telegram state-check \
    --path "${hub_root}" \
    >/dev/null 2>&1 || return 1
  "${telegram_cmd}" telegram health \
    --path "${hub_root}" \
    --timeout "${HEALTH_REQUEST_TIMEOUT_SECONDS}" \
    >/dev/null 2>&1
}

_wait_telegram_healthy() {
  local start now
  start="$(date +%s)"
  while true; do
    if _telegram_check_once; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= HEALTH_TIMEOUT_SECONDS )); then
      return 1
    fi
    sleep "${HEALTH_INTERVAL_SECONDS}"
  done
}

_discord_check_once() {
  local discord_domain pid
  discord_domain="gui/$(id -u)/${DISCORD_LABEL}"
  pid="$(_discord_service_pid)"
  if [[ -n "${pid}" && "${pid}" != "0" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    return 0
  fi
  launchctl print "${discord_domain}" 2>/dev/null | awk '
    /state = running/ {running=1}
    /pid =/ && $3 != "0" {has_pid=1}
    END {exit !(running || has_pid)}
  '
}

_wait_discord_healthy() {
  local start now
  start="$(date +%s)"
  while true; do
    if _discord_check_once; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= HEALTH_TIMEOUT_SECONDS )); then
      return 1
    fi
    sleep "${HEALTH_INTERVAL_SECONDS}"
  done
}

_check_hub_health() {
  if [[ "${should_reload_hub}" != "true" ]]; then
    echo "Skipping hub health check (update target: ${UPDATE_TARGET})."
    return 0
  fi
  if _wait_healthy; then
    echo "Hub health check OK."
    return 0
  fi
  echo "Hub health check failed." >&2
  return 1
}

_check_telegram_health() {
  if [[ "${should_reload_telegram}" != "true" ]]; then
    telegram_health_reason="Telegram update skipped (target: ${UPDATE_TARGET})."
    return 0
  fi
  if ! _should_check_telegram; then
    telegram_health_reason="Telegram health check skipped (disabled or not configured)."
    return 0
  fi
  if _wait_telegram_healthy; then
    telegram_health_checked=true
    telegram_health_reason="Telegram restarted and healthy."
    echo "Telegram health check OK."
    return 0
  fi
    telegram_health_reason="Telegram health check failed."
  echo "Telegram health check failed." >&2
  return 1
}

_check_discord_health() {
  local hub_root discord_state missing_envs
  if [[ "${should_reload_discord}" != "true" ]]; then
    discord_health_reason="Discord update skipped (target: ${UPDATE_TARGET})."
    return 0
  fi

  hub_root="$(_plist_arg_value path)"
  discord_state="$(_discord_state "${hub_root}")"
  if [[ "${discord_state}" == "disabled" ]]; then
    discord_health_reason="Discord health check skipped (disabled or not configured)."
    return 0
  fi
  if [[ "${discord_state}" == "missing_env" ]]; then
    missing_envs="$(_discord_missing_env_names "${hub_root}")"
    if [[ -n "${missing_envs}" ]]; then
      discord_health_reason="Discord health check skipped (missing env vars: ${missing_envs})."
    else
      discord_health_reason="Discord health check skipped (missing required env vars)."
    fi
    return 0
  fi
  if ! _should_check_discord; then
    discord_health_reason="Discord health check skipped (disabled or not configured)."
    return 0
  fi
  if _wait_discord_healthy; then
    discord_health_checked=true
    discord_health_reason="Discord restarted and healthy."
    echo "Discord health check OK."
    return 0
  fi
  discord_health_reason="Discord health check failed."
  echo "Discord health check failed." >&2
  return 1
}

_rollback() {
  local message
  message="$1"
  if [[ "${rollback_completed}" == "true" ]]; then
    return 0
  fi
  rollback_completed=true
  echo "${message}" >&2
  ln -sfn "${current_target}" "${CURRENT_VENV_LINK}"
  if [[ "${should_reload_hub}" == "true" ]]; then
    _reload || true
  fi
  if [[ "${should_reload_telegram}" == "true" ]]; then
    _reload_telegram || true
  fi
  if [[ "${should_reload_discord}" == "true" ]]; then
    _reload_discord || true
  fi
}

_on_exit() {
  local status
  status="$1"
  if [[ "${status}" -eq 0 ]]; then
    return 0
  fi
  if [[ "${swap_completed}" != "true" || "${rollback_completed}" == "true" ]]; then
    return 0
  fi
  _rollback "Update failed; rolling back to ${current_target}..."
  write_status "rollback" "Update failed; rollback attempted."
}

trap '_on_exit $?' EXIT

echo "Switching ${PREV_VENV_LINK} -> ${current_target}"
ln -sfn "${current_target}" "${PREV_VENV_LINK}"

echo "Switching ${CURRENT_VENV_LINK} -> ${next_venv}"
ln -sfn "${next_venv}" "${CURRENT_VENV_LINK}"
swap_completed=true

if [[ "${should_reload_hub}" == "true" ]]; then
  echo "Restarting launchd service ${LABEL}..."
  _ensure_plist_uses_current_venv
  _reload
fi
if [[ "${should_reload_telegram}" == "true" ]]; then
  _reload_telegram
fi
if [[ "${should_reload_discord}" == "true" ]]; then
  _reload_discord
fi

health_ok=true
if ! _check_hub_health; then
  health_ok=false
fi
if ! _check_telegram_health; then
  health_ok=false
fi
if ! _check_discord_health; then
  health_ok=false
fi

if [[ "${health_ok}" == "true" ]]; then
  echo "Health check OK; update successful."
  status_msg="Update completed successfully."
  if [[ "${should_reload_hub}" != "true" ]]; then
    status_msg+=" Hub update skipped (target: ${UPDATE_TARGET})."
  fi
  if [[ -n "${telegram_health_reason}" ]]; then
    status_msg+=" ${telegram_health_reason}"
  fi
  if [[ -n "${discord_health_reason}" ]]; then
    status_msg+=" ${discord_health_reason}"
  fi
  echo "Updating global car CLI..."
  pipx install --force "${PACKAGE_SRC}"
  ensure_login_shell_path "${LOCAL_BIN}"
  write_status "ok" "${status_msg}"
else
  _rollback "Health check failed; rolling back to ${current_target}..."
  rollback_hub_ok=true
  rollback_telegram_ok=true
  rollback_discord_ok=true
  if ! _check_hub_health; then
    rollback_hub_ok=false
  fi
  if ! _check_telegram_health; then
    rollback_telegram_ok=false
  fi
  if ! _check_discord_health; then
    rollback_discord_ok=false
  fi
  if [[ "${rollback_hub_ok}" == "true" && "${rollback_telegram_ok}" == "true" && "${rollback_discord_ok}" == "true" ]]; then
    echo "Rollback OK; service restored." >&2
    write_status "rollback" "Update failed; rollback succeeded."
  else
    failed_checks=()
    if [[ "${rollback_hub_ok}" != "true" ]]; then
      failed_checks+=(hub)
    fi
    if [[ "${rollback_telegram_ok}" != "true" ]]; then
      failed_checks+=(telegram)
    fi
    if [[ "${rollback_discord_ok}" != "true" ]]; then
      failed_checks+=(discord)
    fi
    if (( ${#failed_checks[@]} > 0 )); then
      failed_summary="Health check failed after rollback (${failed_checks[*]})."
    else
      failed_summary="Health check failed after rollback."
    fi
    echo "${failed_summary} Verify service health and logs:" >&2
    echo "  tail -n 200 ~/car-workspace/.codex-autorunner/codex-autorunner-hub.log" >&2
    echo "  launchctl print ${domain}" >&2
    write_status "rollback" \
      "Update failed; rollback completed, but post-rollback health checks failed. Verify service health."
    exit 2
  fi
  exit 1
fi

echo "Pruning old staged venvs (keeping ${KEEP_OLD_VENVS})..."
shopt -s nullglob
staged=( "${PIPX_ROOT}/venvs/codex-autorunner.next-"* )
shopt -u nullglob

if (( ${#staged[@]} > KEEP_OLD_VENVS )); then
  IFS=$'\n' sorted=( $(ls -1dt "${staged[@]}") )
  unset IFS
  to_delete=( "${sorted[@]:${KEEP_OLD_VENVS}}" )
else
  to_delete=()
fi

current_real="$(_realpath "${CURRENT_VENV_LINK}")"
prev_real="$(_realpath "${PREV_VENV_LINK}")"

printf '%s\n' "${to_delete[@]:-}" | while read -r old; do
  if [[ -z "${old}" ]]; then
    continue
  fi
  old_real="$(_realpath "${old}")"
  if [[ -n "${old_real}" && ( "${old_real}" == "${current_real}" || "${old_real}" == "${prev_real}" ) ]]; then
    continue
  fi
  rm -rf "${old}"
done

echo "Done."
