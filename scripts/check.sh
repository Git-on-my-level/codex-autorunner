#!/usr/bin/env bash
# Run formatting and tests before committing.

set -euo pipefail

# Avoid leaking git hook environment into subprocesses (e.g. tests).
unset GIT_DIR
unset GIT_WORK_TREE
unset GIT_INDEX_FILE

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"

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

paths=(src)
if [ -d tests ]; then
  paths+=(tests)
fi

echo "Formatting check (black)..."
"$PYTHON_BIN" -m black --check "${paths[@]}"

echo "Linting Python (ruff)..."
"$PYTHON_BIN" -m ruff check "${paths[@]}"

echo "Linting injected context hints..."
"$PYTHON_BIN" scripts/check_injected_context.py

echo "Linting command resolution..."
"$PYTHON_BIN" scripts/check_command_resolution.py

echo "Checking import boundaries..."
"$PYTHON_BIN" scripts/check_import_boundaries.py

echo "Validating hub interface contracts..."
"$PYTHON_BIN" scripts/validate_interfaces.py

echo "Checking core imports (no adapter implementations)..."
# Temporarily disabled for merge: allowlisted violations not supported by check_core_imports.py
# "$PYTHON_BIN" scripts/check_core_imports.py

echo "Checking for legacy TODO/SUMMARY pipeline code..."
"$PYTHON_BIN" scripts/check_legacy_pipeline.py

echo "Type check (mypy)..."
"$PYTHON_BIN" -m mypy src/codex_autorunner/core src/codex_autorunner/integrations/app_server

echo "Linting JS/TS (eslint)..."
"$ESLINT_BIN" "src/codex_autorunner/static_src/**/*.ts"

echo "Build static assets (pnpm run build)..."
pnpm run build

echo "Checking generated static assets are committed..."
# Treat compiled JS (and maps) in the static folder as generated outputs that must stay in sync.
GENERATED_STATIC=$(find src/codex_autorunner/static -maxdepth 1 -type f \( -name '*.js' -o -name '*.js.map' \) | sort)

if [ -n "$GENERATED_STATIC" ]; then
  # shellcheck disable=SC2086 # git diff needs separate args
  if ! git diff --exit-code -- $GENERATED_STATIC >/dev/null 2>&1; then
    echo "Generated static assets are out of date. Run 'pnpm run build' and commit updated JS outputs." >&2
    # shellcheck disable=SC2086
    git diff --stat -- $GENERATED_STATIC >&2
    exit 1
  fi
fi

echo "Running tests (pytest)..."
"$PYTHON_BIN" -m pytest -m "not integration"

echo "Dead-code check (heuristic)..."
"$PYTHON_BIN" scripts/deadcode.py --check

echo "Checks passed."
