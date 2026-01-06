#!/usr/bin/env bash
# Run formatting and tests before committing.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1. Install dev deps via 'pip install -e .[dev]'." >&2
    exit 1
  fi
}

need_cmd python
need_cmd black
need_cmd ruff
need_cmd mypy
need_cmd pytest
need_cmd node

if [ -x "./node_modules/.bin/eslint" ]; then
  ESLINT_BIN="./node_modules/.bin/eslint"
elif command -v eslint >/dev/null 2>&1; then
  ESLINT_BIN="eslint"
else
  echo "Missing required command: eslint. Install dev deps via 'npm install'." >&2
  exit 1
fi

paths=(src)
if [ -d tests ]; then
  paths+=(tests)
fi

echo "Formatting check (black)..."
python -m black --check "${paths[@]}"

echo "Linting Python (ruff)..."
ruff check "${paths[@]}"

echo "Type check (mypy)..."
mypy src/codex_autorunner/core src/codex_autorunner/integrations/app_server

echo "Linting JS (eslint)..."
"$ESLINT_BIN" "src/codex_autorunner/static/**/*.js"

echo "Running tests (pytest)..."
python -m pytest

echo "Dead-code check (heuristic)..."
python scripts/deadcode.py --check

echo "Checks passed."
