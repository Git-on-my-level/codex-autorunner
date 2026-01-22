#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LEGACY_DIR="$ROOT/.codex-autorunner/pr_flow"
WORKTREES_DIR="$ROOT/.codex-autorunner/worktrees"

MODE="${1:-}"
APPLY=0
if [[ "$MODE" == "--apply" ]]; then
  APPLY=1
fi

if [[ $APPLY -eq 1 ]]; then
  echo "PR flow legacy cleanup (apply mode)."
else
  echo "PR flow legacy cleanup (dry-run). Use --apply to remove."
fi

if [[ -d "$LEGACY_DIR" ]]; then
  echo "Legacy PR flow dir: $LEGACY_DIR"
  if [[ $APPLY -eq 1 ]]; then
    rm -rf "$LEGACY_DIR"
    echo "Removed legacy PR flow dir."
  fi
else
  echo "Legacy PR flow dir not found."
fi

if [[ -d "$WORKTREES_DIR" ]]; then
  if ! worktree_output="$(git -C "$ROOT" worktree list --porcelain 2>/dev/null)"; then
    echo "Failed to read git worktree list; aborting."
    exit 1
  fi
  mapfile -t registered_worktrees < <(printf "%s\n" "$worktree_output" | awk '/^worktree /{print $2}')
  shopt -s nullglob
  echo "Scanning worktrees under $WORKTREES_DIR"
  for dir in "$WORKTREES_DIR"/*; do
    [[ -d "$dir" ]] || continue
    found=0
    for wt in "${registered_worktrees[@]}"; do
      if [[ "$wt" == "$dir" ]]; then
        found=1
        break
      fi
    done
    if [[ $found -eq 0 ]]; then
      echo "Stale worktree dir: $dir"
      if [[ $APPLY -eq 1 ]]; then
        rm -rf "$dir"
        echo "Removed $dir"
      fi
    fi
  done
  shopt -u nullglob
else
  echo "Worktrees dir not found."
fi
