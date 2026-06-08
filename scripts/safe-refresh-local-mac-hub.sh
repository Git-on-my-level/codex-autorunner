#!/usr/bin/env bash
set -euo pipefail

# Thin compatibility wrapper: update orchestration lives in the Python
# UpdateEngine (codex_autorunner.core.update.engine), which owns staged venv
# install, atomic CURRENT_VENV_LINK cutover, launchd plist normalization,
# OpenCode PATH injection, chat (telegram/discord) enable/disable, DB snapshot,
# health gates, rollback, car-wrapper sync, and old-venv prune.
#
# The hub normally spawns the worker directly:
#   python -m codex_autorunner.core.update.runner --backend launchd ...
#
# This script remains for manual/emergency invocation and delegates to the same
# engine. The mac-specific knobs below are read from the environment by the
# engine (launchd labels/plists, OpenCode/NVM/PATH dirs, snapshot retention).
#
# Environment knobs (engine reads these from the environment):
#   PACKAGE_SRC            Path to this repo / update cache (default: scripts/..)
#   REPO_URL               Update source remote (default: cursor/codex-autorunner)
#   REPO_REF               Ref to update to (default: main)
#   UPDATE_STATUS_PATH     Status JSON path the engine writes (required)
#   UPDATE_TARGET          Which services to restart (all|web|chat|telegram|discord; default: all)
#   HELPER_PYTHON          Python with codex_autorunner installed (default: CURRENT_VENV_LINK/bin/python, then python3)
#   LABEL / PLIST_PATH     Hub launchd label and plist path
#   TELEGRAM_LABEL / TELEGRAM_PLIST_PATH / ENABLE_TELEGRAM_BOT
#   DISCORD_LABEL / DISCORD_PLIST_PATH / ENABLE_DISCORD_BOT
#   PIPX_ROOT / PIPX_VENV / PIPX_PYTHON
#   CURRENT_VENV_LINK / PREV_VENV_LINK
#   OPENCODE_BIN / NVM_BIN / LOCAL_BIN / PY39_BIN
#   KEEP_OLD_VENVS / LAUNCHD_STOP_WAIT_SECONDS / UPDATE_SNAPSHOT_MAX_COUNT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_SRC="${PACKAGE_SRC:-$SCRIPT_DIR/..}"
UPDATE_STATUS_PATH="${UPDATE_STATUS_PATH:-}"
UPDATE_TARGET="${UPDATE_TARGET:-all}"

PIPX_ROOT="${PIPX_ROOT:-$HOME/.local/pipx}"
CURRENT_VENV_LINK="${CURRENT_VENV_LINK:-$PIPX_ROOT/venvs/codex-autorunner.current}"
HELPER_PYTHON="${HELPER_PYTHON:-${PIPX_PYTHON:-}}"

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  if [[ -x "${CURRENT_VENV_LINK}/bin/python" ]]; then
    HELPER_PYTHON="${CURRENT_VENV_LINK}/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    HELPER_PYTHON="$(command -v python)"
  fi
fi

if [[ -z "${HELPER_PYTHON}" || ! -x "${HELPER_PYTHON}" ]]; then
  echo "Python not found (set HELPER_PYTHON)." >&2
  exit 1
fi

# The engine resolves the canonical status path itself; UPDATE_STATUS_PATH only
# pins where the standalone log is written for manual runs.
if [[ -n "${UPDATE_STATUS_PATH}" ]]; then
  LOG_PATH="$(dirname "${UPDATE_STATUS_PATH}")/update-standalone.log"
else
  LOG_PATH="${TMPDIR:-/tmp}/codex-autorunner-update.log"
fi
REPO_URL="${REPO_URL:-https://github.com/cursor/codex-autorunner.git}"
REPO_REF="${REPO_REF:-main}"

export HELPER_PYTHON PACKAGE_SRC UPDATE_STATUS_PATH UPDATE_TARGET CURRENT_VENV_LINK PIPX_ROOT

exec "${HELPER_PYTHON}" -m codex_autorunner.core.update.runner \
  --repo-url "${REPO_URL}" \
  --repo-ref "${REPO_REF}" \
  --update-dir "${PACKAGE_SRC}" \
  --log-path "${LOG_PATH}" \
  --target "${UPDATE_TARGET}" \
  --backend launchd \
  --skip-checks
