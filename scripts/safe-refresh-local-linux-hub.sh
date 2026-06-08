#!/usr/bin/env bash
set -euo pipefail

# Thin compatibility wrapper: update orchestration lives in UpdateEngine.
#
# The hub worker clones source and runs:
#   python -m codex_autorunner.core.update.runner
#
# This script remains for manual/emergency use and preserves the env contract
# expected by older tooling. It delegates to the Python engine when invoked
# directly with PACKAGE_SRC and UPDATE_STATUS_PATH set.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"
UPDATE_STATUS_PATH="${UPDATE_STATUS_PATH:-}"
UPDATE_TARGET="${UPDATE_TARGET:-all}"
UPDATE_BACKEND="${UPDATE_BACKEND:-auto}"
HELPER_PYTHON="${HELPER_PYTHON:-}"

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python)"
  fi
fi

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  echo "Python not found (set HELPER_PYTHON)." >&2
  exit 1
fi

if [[ -z "${UPDATE_STATUS_PATH}" ]]; then
  echo "UPDATE_STATUS_PATH is required for the Linux refresh wrapper." >&2
  exit 1
fi

LOG_PATH="$(dirname "${UPDATE_STATUS_PATH}")/update-standalone.log"
REPO_URL="${REPO_URL:-https://github.com/cursor/codex-autorunner.git}"
REPO_REF="${REPO_REF:-main}"

PACKAGE_SRC_ROOT="$(cd "${PACKAGE_SRC}" && pwd)"
PACKAGE_SRC_PYTHONPATH="${PACKAGE_SRC_ROOT}/src"
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${PACKAGE_SRC_PYTHONPATH}:${PYTHONPATH}"
else
  export PYTHONPATH="${PACKAGE_SRC_PYTHONPATH}"
fi
export HELPER_PYTHON
export PACKAGE_SRC
export UPDATE_STATUS_PATH
export UPDATE_TARGET
export UPDATE_BACKEND
export SYSTEMD_SCOPE="${SYSTEMD_SCOPE:-}"
export UPDATE_SYSTEMCTL_SUDO="${UPDATE_SYSTEMCTL_SUDO:-auto}"
export UPDATE_HUB_SERVICE_NAME="${UPDATE_HUB_SERVICE_NAME:-car-hub}"
export UPDATE_TELEGRAM_SERVICE_NAME="${UPDATE_TELEGRAM_SERVICE_NAME:-car-telegram}"
export UPDATE_DISCORD_SERVICE_NAME="${UPDATE_DISCORD_SERVICE_NAME:-car-discord}"

exec "${HELPER_PYTHON}" -m codex_autorunner.core.update.runner \
  --repo-url "${REPO_URL}" \
  --repo-ref "${REPO_REF}" \
  --update-dir "${PACKAGE_SRC}" \
  --log-path "${LOG_PATH}" \
  --target "${UPDATE_TARGET}" \
  --backend "${UPDATE_BACKEND}" \
  --hub-service-name "${UPDATE_HUB_SERVICE_NAME}" \
  --telegram-service-name "${UPDATE_TELEGRAM_SERVICE_NAME}" \
  --discord-service-name "${UPDATE_DISCORD_SERVICE_NAME}" \
  --skip-checks
