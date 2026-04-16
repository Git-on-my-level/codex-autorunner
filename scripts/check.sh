#!/usr/bin/env bash
# Lane-aware validation runner.
#
# Usage:
#   ./scripts/check.sh                  # auto-detect lane from staged files
#   ./scripts/check.sh --full           # force full safety suite (all checks)
#   ./scripts/check.sh --lane <lane>    # run checks for a specific lane
#
# Lanes: core, web-ui, chat-apps, aggregate (= full).
# When no flag is given, staged files are classified via the shared lane
# classifier (scripts/select_validation_lane.py).

set -euo pipefail

unset GIT_DIR
unset GIT_WORK_TREE
unset GIT_INDEX_FILE

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"

# --- Argument parsing --------------------------------------------------------
LANE=""
FULL_MODE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      FULL_MODE=true
      shift
      ;;
    --lane)
      LANE="${2:-}"
      if [[ -z "$LANE" ]]; then
        echo "--lane requires an argument (core|web-ui|chat-apps|aggregate)" >&2
        exit 1
      fi
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$FULL_MODE" == true ]]; then
  LANE="aggregate"
fi

# --- Hooks guard (local only) ------------------------------------------------
if [ -z "${CI:-}" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HOOKS_PATH="$(git config --get core.hooksPath || true)"
  case "$HOOKS_PATH" in
    .githooks|*/.githooks)
      ;;
    *)
      echo "Git hooks are not installed for this repo/worktree." >&2
      echo "Run 'make hooks' (or 'make setup') to enable pre-commit checks." >&2
      exit 1
      ;;
  esac
fi

# --- Tool resolution ---------------------------------------------------------
need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1. Install dev deps via 'pip install -e .[dev]'." >&2
    exit 1
  fi
}

PYTHON_BIN="python"
if [ -f ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
need_cmd "$PYTHON_BIN"
need_cmd make

# --- Lane detection (if not explicitly set) ----------------------------------
if [[ -z "$LANE" ]]; then
  STAGED_FILES="$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)"
  if [[ -n "$STAGED_FILES" ]]; then
    LANE="$(echo "$STAGED_FILES" | "$PYTHON_BIN" scripts/select_validation_lane.py --format json | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["lane"])')"
  else
    LANE="aggregate"
  fi
  echo "Detected validation lane: $LANE"
fi

echo "Validation lane: $LANE"

# --- Resolve lane flags ------------------------------------------------------
# aggregate => run everything (equivalent to --full)
RUN_CORE=true
RUN_WEB_UI=false
RUN_CHAT_APPS=false

case "$LANE" in
  core)
    RUN_CORE=true
    RUN_WEB_UI=false
    RUN_CHAT_APPS=false
    ;;
  web-ui)
    RUN_CORE=true
    RUN_WEB_UI=true
    RUN_CHAT_APPS=false
    ;;
  chat-apps)
    RUN_CORE=true
    RUN_WEB_UI=false
    RUN_CHAT_APPS=true
    ;;
  aggregate)
    RUN_CORE=true
    RUN_WEB_UI=true
    RUN_CHAT_APPS=true
    ;;
  *)
    echo "Unknown lane: $LANE. Falling back to aggregate (full checks)." >&2
    RUN_CORE=true
    RUN_WEB_UI=true
    RUN_CHAT_APPS=true
    ;;
esac

# --- Always-on guardrails (non-negotiable safety checks) ---------------------
echo "Checking staged .codex-autorunner paths..."
"$PYTHON_BIN" scripts/check_no_codex_autorunner_staged.py

paths=(src)
if [ -d tests ]; then
  paths+=(tests)
fi

echo "Formatting check (black)..."
"$PYTHON_BIN" -m black --check "${paths[@]}"

echo "Linting Python (ruff)..."
"$PYTHON_BIN" -m ruff check "${paths[@]}"

echo "Checking import boundaries..."
"$PYTHON_BIN" scripts/check_import_boundaries.py

echo "Validating hub interface contracts..."
"$PYTHON_BIN" scripts/validate_interfaces.py

echo "Checking core imports (no adapter implementations)..."
"$PYTHON_BIN" scripts/check_core_imports.py

echo "Checking for legacy TODO/SUMMARY pipeline code..."
"$PYTHON_BIN" scripts/check_legacy_pipeline.py

echo "Checking destination contract drift..."
"$PYTHON_BIN" scripts/check_destination_contract_drift.py

echo "Checking keyword contracts..."
"$PYTHON_BIN" scripts/check_keyword_contracts.py --report-only

echo "Checking test /tmp hermetic usage..."
"$PYTHON_BIN" scripts/check_test_tmp_usage.py

# --- Core lane checks --------------------------------------------------------
if [[ "$RUN_CORE" == true ]]; then
  echo "Linting injected context hints..."
  "$PYTHON_BIN" scripts/check_injected_context.py

  echo "Linting command resolution..."
  "$PYTHON_BIN" scripts/check_command_resolution.py

  echo "Validating CLI command hints..."
  "$PYTHON_BIN" scripts/check_cli_command_hints.py

  echo "Type check (mypy, strict repo-wide)..."
  make typecheck-strict PYTHON="$PYTHON_BIN"

  echo "Running tests (pytest)..."
  if [ -z "${CI:-}" ]; then
    FAST_TEST_JUNIT="$(mktemp)"
    cleanup_fast_test_artifacts() {
      rm -f "$FAST_TEST_JUNIT" "${FAST_TEST_SELECTED:-}"
    }
    trap cleanup_fast_test_artifacts EXIT
    "$PYTHON_BIN" -m pytest -m "not integration and not slow" -n auto -o junit_duration_report=call --junitxml "$FAST_TEST_JUNIT"
    FAST_TEST_REPORT_ARGS=(
      "$FAST_TEST_JUNIT"
      --repo-root "$REPO_ROOT"
      --max-duration "${CODEX_FAST_TEST_MAX_DURATION_SECONDS:-1.0}"
      --max-report "${CODEX_FAST_TEST_REPORT_LIMIT:-20}"
    )
    if [[ "${CODEX_FAST_TEST_VERIFY_NODEIDS:-0}" == "1" ]]; then
      FAST_TEST_SELECTED="$(mktemp)"
      "$PYTHON_BIN" -m pytest -m "not integration and not slow" --collect-only -q > "$FAST_TEST_SELECTED"
      FAST_TEST_REPORT_ARGS+=(
        --selected-nodeids "$FAST_TEST_SELECTED"
        --verify-nodeids
      )
    fi
    "$PYTHON_BIN" scripts/report_fast_test_budget.py "${FAST_TEST_REPORT_ARGS[@]}"
  else
    "$PYTHON_BIN" -m pytest -m "not integration and not slow" -n auto
  fi

  if [ -n "${CI:-}" ] || [[ "${CODEX_LOCAL_CHECK_INCLUDE_DEADCODE:-0}" == "1" ]]; then
    echo "Dead-code check (heuristic)..."
    "$PYTHON_BIN" "$REPO_ROOT/scripts/deadcode.py" --check
  else
    echo "Skipping dead-code check locally; set CODEX_LOCAL_CHECK_INCLUDE_DEADCODE=1 to enable."
  fi
fi

# --- Web-UI lane checks ------------------------------------------------------
if [[ "$RUN_WEB_UI" == true ]]; then
  need_cmd node
  need_cmd pnpm

  if [ -x "./node_modules/.bin/eslint" ]; then
    ESLINT_BIN="./node_modules/.bin/eslint"
  elif command -v eslint >/dev/null 2>&1; then
    ESLINT_BIN="eslint"
  else
    echo "Missing required command: eslint. Install dev deps via 'pnpm install'." >&2
    exit 1
  fi

  echo "Linting JS/TS (eslint)..."
  "$ESLINT_BIN" "src/codex_autorunner/static_src/**/*.ts"

  echo "Build static assets (pnpm run build)..."
  pnpm run build

  echo "Running frontend JS tests (pnpm test:markdown)..."
  pnpm test:markdown

  echo "Checking asset manifest is committed..."
  if [ -f src/codex_autorunner/static/assets.json ]; then
    if ! git diff --exit-code -- src/codex_autorunner/static/assets.json >/dev/null 2>&1; then
      echo "Asset manifest is out of date. Run 'pnpm run build' and commit updated manifest." >&2
      git diff --stat -- src/codex_autorunner/static/assets.json >&2
      exit 1
    fi
  fi
fi

# --- Chat-apps lane hook (future chat-specific checks go here) ---------------
# Currently chat-apps reuses core checks; add chat-specific linters/tests here
# as the surface grows.  The aggregate lane sets this flag true as well.

echo "Checks passed (lane: $LANE)."
echo "Optional extended checks: make test-chat-platform-contract"
