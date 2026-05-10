#!/usr/bin/env bash
# Development hub: FastAPI with Python reload + Web Vite dev server (HMR).
# Open the Vite URL for UI work; it proxies /hub, /api, etc. to the hub process.
#
# Requires `.codex-autorunner/config.yml`. Override with CAR_ROOT; Makefile pins
# CAR_HUB_ROOT to CAR_ROOT unless the caller explicitly passes CAR_HUB_ROOT.
#
# Troubleshooting:
#   CAR_SERVE_NO_RELOAD=1 make serve   — single process (no --reload) if the worker never binds.
#   CAR_SERVE_DEBUG=1 make serve       — uvicorn --log-level debug
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4173}"
WEB_DEV_PORT="${WEB_DEV_PORT:-5173}"
CAR_ROOT="${CAR_ROOT:-$HOME/car-workspace}"

resolve_car_hub_root() {
  if [[ -n "${CAR_HUB_ROOT:-}" && -f "$CAR_HUB_ROOT/.codex-autorunner/config.yml" ]]; then
    printf '%s\n' "$CAR_HUB_ROOT"
    return 0
  fi
  if [[ -f "$CAR_ROOT/.codex-autorunner/config.yml" ]]; then
    printf '%s\n' "$CAR_ROOT"
    return 0
  fi
  if [[ -f "$ROOT/.codex-autorunner/config.yml" ]]; then
    printf '%s\n' "$ROOT"
    return 0
  fi
  return 1
}

if ! CAR_HUB_ROOT="$(resolve_car_hub_root)"; then
  echo "Could not find .codex-autorunner/config.yml for the hub." >&2
  echo "  Tried CAR_HUB_ROOT=${CAR_HUB_ROOT:-<unset>}" >&2
  echo "  Tried CAR_ROOT=$CAR_ROOT" >&2
  echo "  Tried repo root=$ROOT" >&2
  echo "Set CAR_ROOT or CAR_HUB_ROOT to an initialized hub directory, or run:" >&2
  echo "  car init --mode hub <directory>" >&2
  exit 1
fi
export CAR_HUB_ROOT

if [[ "$CAR_HUB_ROOT" == "$ROOT" ]] && [[ "$CAR_ROOT" != "$ROOT" ]]; then
  echo "==> CAR_HUB_ROOT=$CAR_HUB_ROOT (hub config inside this repo checkout)" >&2
fi

if command -v lsof >/dev/null 2>&1; then
  for p in "$PORT" "$WEB_DEV_PORT"; do
    pid="$(lsof -t -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -n "${pid:-}" ]]; then
      echo "Port $p is already in use by PID $pid. Stop the existing process first." >&2
      exit 1
    fi
  done
fi

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "==> Preflight: loading hub app (same as uvicorn factory)..." >&2
export PYTHONUNBUFFERED=1
export CAR_DEV_INCLUDE_ROOT_REPO=1
set +e
"$PYTHON" -u <<'PY'
import os
import traceback

os.environ.setdefault("CAR_DEV_INCLUDE_ROOT_REPO", "1")
try:
    from codex_autorunner.server import create_hub_app

    create_hub_app()
    print("preflight_ok")
except BaseException:
    traceback.print_exc()
    raise SystemExit(1)
PY
preflight_rc=$?
set -e
if [[ "$preflight_rc" -ne 0 ]]; then
  echo "" >&2
  echo "Preflight failed (exit ${preflight_rc}). Fix the error above; uvicorn will not start." >&2
  exit "$preflight_rc"
fi

HUB_LOG=""
cleanup() {
  if [[ -n "${HUB_PID:-}" ]] && kill -0 "$HUB_PID" 2>/dev/null; then
    kill "$HUB_PID" 2>/dev/null || true
    wait "$HUB_PID" 2>/dev/null || true
  fi
  if [[ -n "${HUB_LOG:-}" && -f "${HUB_LOG:-}" ]]; then
    rm -f "$HUB_LOG"
  fi
}
trap cleanup EXIT INT TERM

HUB_LOG="$(mktemp)"

UVICORN_LOG_LEVEL="${CAR_SERVE_DEBUG:+debug}"
UVICORN_LOG_LEVEL="${UVICORN_LOG_LEVEL:-info}"

# NOTE: Do not pass --reload-exclude here. Uvicorn/watchfiles can hang indefinitely on
# startup with exclude globs (no listener, empty logs). Reload is already scoped by
# --reload-dir to codex-autorunner/src only.
RELOAD_ARGS=(
  --reload
  --reload-dir "$ROOT/src"
  --reload-include '*.py'
  --reload-include '*.js'
  --reload-include '*.css'
  --reload-include '*.html'
  --reload-include '*.json'
)
if [[ "${CAR_SERVE_NO_RELOAD:-}" == "1" ]]; then
  echo "==> CAR_SERVE_NO_RELOAD=1 — uvicorn without --reload (Python changes need restart)" >&2
  RELOAD_ARGS=()
fi

{
  echo "[serve-dev-hub] starting uvicorn at $(date -Iseconds 2>/dev/null || date)"
  cd "$ROOT"
  export PYTHONUNBUFFERED=1
  export CAR_DEV_INCLUDE_ROOT_REPO=1
  exec "$PYTHON" -u -m uvicorn codex_autorunner.server:create_hub_app \
    --factory \
    "${RELOAD_ARGS[@]}" \
    --host "$HOST" \
    --port "$PORT" \
    --log-level "$UVICORN_LOG_LEVEL" \
    --timeout-graceful-shutdown 1
} >"$HUB_LOG" 2>&1 &
HUB_PID=$!

# Probe loopback URL — Host header matches HostOriginMiddleware defaults when hub uses configured allowed_hosts.
BASE_URL="${CAR_HUB_PROBE_URL:-http://localhost:${PORT}}"

hub_http_ok() {
  local url=$1
  local code
  code="$(curl -sS -m 4 -L -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || printf '000')"
  [[ "$code" == "200" ]]
}

hub_ready=0
for _ in $(seq 1 200); do
  if ! kill -0 "$HUB_PID" 2>/dev/null; then
    echo "Hub process exited before becoming ready. Log:" >&2
    tail -n 200 "$HUB_LOG" >&2
    exit 1
  fi
  if ! (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
    sleep 0.2
    continue
  fi
  if command -v curl >/dev/null 2>&1; then
    for path in ${CAR_HUB_HEALTH_PATHS:-/health /car/health}; do
      if hub_http_ok "${BASE_URL}${path}"; then
        hub_ready=1
        break 2
      fi
    done
  elif command -v nc >/dev/null 2>&1; then
    hub_ready=1
    break
  fi
  sleep 0.2
done
if [[ "$hub_ready" -ne 1 ]]; then
  echo "Timed out waiting for hub HTTP 200 on ${BASE_URL}/health (and fallbacks)." >&2
  echo "Probe paths: ${CAR_HUB_HEALTH_PATHS:-/health /car/health}; override base with CAR_HUB_PROBE_URL=http://127.0.0.1:${PORT}" >&2
  echo "If your hub sets server_allowed_hosts, try CAR_HUB_PROBE_URL matching that host." >&2
  echo "CAR_HUB_ROOT=${CAR_HUB_ROOT}" >&2
  if command -v lsof >/dev/null 2>&1; then
    echo "--- listeners on ${PORT} ---" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  fi
  echo "--- uvicorn log (full file, max 200 lines) ---" >&2
  tail -n 200 "$HUB_LOG" >&2
  echo "" >&2
  echo "Tip: CAR_SERVE_NO_RELOAD=1 make serve  or  CAR_SERVE_DEBUG=1 make serve" >&2
  exit 1
fi

rm -f "$HUB_LOG"
HUB_LOG=""

export CAR_HUB_PROXY_TARGET="http://127.0.0.1:${PORT}"

echo ""
echo "==> Hub API (Python reload): http://${HOST}:${PORT}"
echo "==> Web UI (Vite HMR):       http://${HOST}:${WEB_DEV_PORT}/chats"
echo ""

pnpm web:dev -- --host "$HOST" --port "$WEB_DEV_PORT"
