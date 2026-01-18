#!/usr/bin/env bash
# Run formatting and tests before committing.

set -euo pipefail

# Avoid leaking git hook environment into subprocesses (e.g. tests).
unset GIT_DIR
unset GIT_WORK_TREE
unset GIT_INDEX_FILE

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

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

if [ -x "./node_modules/.bin/eslint" ]; then
  ESLINT_BIN="./node_modules/.bin/eslint"
elif command -v eslint >/dev/null 2>&1; then
  ESLINT_BIN="eslint"
else
  echo "Missing required command: eslint. Install dev deps via 'pnpm install'." >&2
  exit 1
fi
if [ -x "./node_modules/.bin/tsc" ]; then
  TSC_BIN="./node_modules/.bin/tsc"
elif command -v tsc >/dev/null 2>&1; then
  TSC_BIN="tsc"
else
  echo "Missing required command: tsc. Install dev deps via 'pnpm install'." >&2
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

echo "Checking work docs..."
"$PYTHON_BIN" scripts/check_docs.py

echo "Type check (mypy)..."
"$PYTHON_BIN" -m mypy src/codex_autorunner/core src/codex_autorunner/integrations/app_server

echo "Linting JS/TS (eslint)..."
"$ESLINT_BIN" "src/codex_autorunner/static/**/*.js" "src/codex_autorunner/static_src/**/*.ts"

echo "Type check (tsc)..."
"$TSC_BIN" -p tsconfig.json

echo "Checking static build outputs are committed..."
if ! git diff --exit-code -- src/codex_autorunner/static >/dev/null 2>&1; then
  echo "Static assets are out of date. Run 'pnpm run build' and commit outputs." >&2
  git diff --stat -- src/codex_autorunner/static >&2
  exit 1
fi

echo "Running tests (pytest)..."
"$PYTHON_BIN" -m pytest -m "not integration"

echo "Dead-code check (heuristic)..."
"$PYTHON_BIN" scripts/deadcode.py --check

echo "Checks passed."
