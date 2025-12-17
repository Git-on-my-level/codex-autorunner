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
#   PIPX_ROOT              pipx root (default: ~/.local/pipx)
#   PIPX_VENV              existing pipx venv path (default: ${PIPX_ROOT}/venvs/codex-autorunner)
#   CURRENT_VENV_LINK      symlink path used by launchd (default: ${PIPX_ROOT}/venvs/codex-autorunner.current)
#   PREV_VENV_LINK         symlink path used for rollback (default: ${PIPX_ROOT}/venvs/codex-autorunner.prev)
#   HEALTH_TIMEOUT_SECONDS seconds to wait for health (default: 30)
#   HEALTH_INTERVAL_SECONDS poll interval (default: 0.5)
#   HEALTH_PATH            request path (default: /car/openapi.json)
#   KEEP_OLD_VENVS         how many old next-* venvs to keep (default: 3)

LABEL="${LABEL:-com.codex.autorunner}"
PLIST_PATH="${PLIST_PATH:-$HOME/Library/LaunchAgents/${LABEL}.plist}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"

PIPX_ROOT="${PIPX_ROOT:-$HOME/.local/pipx}"
PIPX_VENV="${PIPX_VENV:-$PIPX_ROOT/venvs/codex-autorunner}"
CURRENT_VENV_LINK="${CURRENT_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.current}"
PREV_VENV_LINK="${PREV_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.prev}"

HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-0.5}"
HEALTH_PATH="${HEALTH_PATH:-/car/openapi.json}"
KEEP_OLD_VENVS="${KEEP_OLD_VENVS:-3}"

if [[ ! -f "${PLIST_PATH}" ]]; then
  echo "LaunchAgent plist not found at ${PLIST_PATH}" >&2
  echo "Run scripts/install-local-mac-hub.sh or scripts/launchd-hub.sh first (or set PLIST_PATH)." >&2
  exit 1
fi

if [[ ! -d "${PIPX_VENV}" ]]; then
  echo "Expected pipx venv not found at ${PIPX_VENV}" >&2
  echo "Run scripts/install-local-mac-hub.sh first (or set PIPX_VENV)." >&2
  exit 1
fi

PIPX_PYTHON="${PIPX_PYTHON:-$PIPX_VENV/bin/python}"
if [[ ! -x "${PIPX_PYTHON}" ]]; then
  echo "Python not found at ${PIPX_PYTHON}" >&2
  exit 1
fi

if [[ ! -L "${CURRENT_VENV_LINK}" ]]; then
  echo "Initializing ${CURRENT_VENV_LINK} -> ${PIPX_VENV}"
  ln -sfn "${PIPX_VENV}" "${CURRENT_VENV_LINK}"
fi

current_target="$("${PIPX_PYTHON}" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${CURRENT_VENV_LINK}")"

ts="$(date +%Y%m%d-%H%M%S)"
next_venv="${PIPX_ROOT}/venvs/codex-autorunner.next-${ts}"

echo "Creating staged venv at ${next_venv} (python: ${PIPX_PYTHON})..."
"${PIPX_PYTHON}" -m venv "${next_venv}"
"${next_venv}/bin/python" -m pip -q install --upgrade pip

echo "Installing codex-autorunner from ${PACKAGE_SRC} into staged venv..."
"${next_venv}/bin/python" -m pip -q install --force-reinstall "${PACKAGE_SRC}"

echo "Smoke-checking staged venv imports..."
"${next_venv}/bin/python" -c "import codex_autorunner; from codex_autorunner.server import create_hub_app; print('ok')"

domain="gui/$(id -u)/${LABEL}"

_ensure_plist_uses_current_venv() {
  local desired_bin
  desired_bin="${CURRENT_VENV_LINK}/bin/codex-autorunner"

  if grep -q "${desired_bin}" "${PLIST_PATH}"; then
    return 0
  fi

  echo "Updating plist to use ${desired_bin}..."
  "${PIPX_PYTHON}" - <<PY
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

_reload() {
  launchctl unload -w "${PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl load -w "${PLIST_PATH}" >/dev/null
  launchctl kickstart -k "${domain}" >/dev/null
}

_health_check_once() {
  local port url
  port="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "${PLIST_PATH}" | head -n1 || true)"
  if [[ -z "${port}" ]]; then
    port="4173"
  fi
  # Always use loopback; hub may bind 0.0.0.0. HEALTH_PATH is absolute.
  url="http://127.0.0.1:${port}${HEALTH_PATH}"
  curl -fsS "${url}" >/dev/null 2>&1
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

echo "Switching ${PREV_VENV_LINK} -> ${current_target}"
ln -sfn "${current_target}" "${PREV_VENV_LINK}"

echo "Switching ${CURRENT_VENV_LINK} -> ${next_venv}"
ln -sfn "${next_venv}" "${CURRENT_VENV_LINK}"

echo "Restarting launchd service ${LABEL}..."
_ensure_plist_uses_current_venv
_reload

if _wait_healthy; then
  echo "Health check OK; update successful."
else
  echo "Health check failed; rolling back to ${current_target}..." >&2
  ln -sfn "${current_target}" "${CURRENT_VENV_LINK}"
  _reload || true
  if _wait_healthy; then
    echo "Rollback OK; service restored." >&2
  else
    echo "Rollback failed; service still unhealthy. Check logs and launchctl state:" >&2
    echo "  tail -n 200 ~/car-workspace/.codex-autorunner/codex-autorunner-hub.log" >&2
    echo "  launchctl print ${domain}" >&2
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

current_real="$("${PIPX_PYTHON}" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${CURRENT_VENV_LINK}")"
prev_real="$("${PIPX_PYTHON}" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${PREV_VENV_LINK}" 2>/dev/null || true)"

printf '%s\n' "${to_delete[@]:-}" | while read -r old; do
  if [[ -z "${old}" ]]; then
    continue
  fi
  old_real="$("${PIPX_PYTHON}" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${old}" 2>/dev/null || true)"
  if [[ -n "${old_real}" && ( "${old_real}" == "${current_real}" || "${old_real}" == "${prev_real}" ) ]]; then
    continue
  fi
  rm -rf "${old}"
done

echo "Done."
