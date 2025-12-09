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
need_cmd pytest

paths=(src)
if [ -d tests ]; then
  paths+=(tests)
fi

echo "Formatting check (black)..."
python -m black --check "${paths[@]}"

echo "Running tests (pytest)..."
python -m pytest

echo "Checks passed."
