#!/usr/bin/env bash
set -euo pipefail

HELPER_PYTHON="${HELPER_PYTHON:-}"
HUB_ROOT="${HUB_ROOT:-}"

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

if [[ -z "${HUB_ROOT}" ]]; then
  echo "HUB_ROOT is required." >&2
  exit 1
fi

"${HELPER_PYTHON}" -m codex_autorunner.core.managed_repo_update --path "${HUB_ROOT}"
