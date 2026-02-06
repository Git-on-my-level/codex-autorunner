#!/usr/bin/env bash
set -euo pipefail

# Migration script: .codex-autorunner/workspace/ → .codex-autorunner/contextspace/
#
# This script migrates existing repo-local shared docs from the old workspace
# directory to the new contextspace directory.
#
# Args:
#   --repo <path>    Repo path (default: current directory)
#   --dry-run        Show what would happen without making changes
#   -h, --help       Show help
#
# The script is idempotent and safe:
# - If old dir is missing: prints "nothing to do" and exits 0
# - If new dir already exists and old is empty: removes old and exits 0
# - If new dir is empty and old has content: moves old → new
# - If both dirs are non-empty: fails loudly with instructions

REPO_PATH=""
DRY_RUN=""

show_help() {
    cat <<EOF
Usage: migrate-workspace-to-contextspace.sh [OPTIONS]

Migrate .codex-autorunner/workspace/ to .codex-autorunner/contextspace/

OPTIONS:
    --repo <path>     Repo path (default: current directory)
    --dry-run         Show what would happen without making changes
    -h, --help        Show this help

EXAMPLES:
    # Migrate current directory
    ./scripts/migrate-workspace-to-contextspace.sh

    # Migrate specific repo
    ./scripts/migrate-workspace-to-contextspace.sh --repo /path/to/repo

    # Dry run to see what would happen
    ./scripts/migrate-workspace-to-contextspace.sh --dry-run
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO_PATH="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage" >&2
            exit 1
            ;;
    esac
done

# Default to current directory
if [[ -z "$REPO_PATH" ]]; then
    REPO_PATH="$(pwd)"
fi

# Validate repo path
if [[ ! -d "$REPO_PATH" ]]; then
    echo "Error: Repo path does not exist: $REPO_PATH" >&2
    exit 1
fi

CAR_DIR="$REPO_PATH/.codex-autorunner"
OLD_DIR="$CAR_DIR/workspace"
NEW_DIR="$CAR_DIR/contextspace"

# Check if repo has .codex-autorunner directory
if [[ ! -d "$CAR_DIR" ]]; then
    echo "Nothing to do: .codex-autorunner/ directory not found"
    exit 0
fi

# Case 1: old dir missing
if [[ ! -d "$OLD_DIR" ]]; then
    echo "Nothing to do: workspace directory not found (migration may have already been done)"
    exit 0
fi

# Check if old dir is empty
OLD_CONTENT=$(ls -A "$OLD_DIR" 2>/dev/null || true)
OLD_EMPTY=$([[ -z "$OLD_CONTENT" ]] && echo "true" || echo "false")

# Case 2: new dir missing, old has content
if [[ ! -d "$NEW_DIR" ]]; then
    if [[ "$OLD_EMPTY" == "true" ]]; then
        echo "Removing empty workspace directory..."
        if [[ -z "$DRY_RUN" ]]; then
            rmdir "$OLD_DIR"
        fi
        echo "Done: removed empty workspace directory"
    else
        echo "Moving workspace to contextspace..."
        echo "Files to move:"
        ls -la "$OLD_DIR"
        if [[ -z "$DRY_RUN" ]]; then
            mkdir -p "$NEW_DIR"
            mv "$OLD_DIR"/* "$NEW_DIR"/
            rmdir "$OLD_DIR"
        fi
        echo "Done: moved workspace to contextspace"
    fi
    exit 0
fi

# Case 3: both dirs exist, old is empty
if [[ "$OLD_EMPTY" == "true" ]]; then
    echo "Removing empty workspace directory..."
    if [[ -z "$DRY_RUN" ]]; then
        rmdir "$OLD_DIR"
    fi
    echo "Done: removed empty workspace directory"
    exit 0
fi

# Case 4: new dir is empty, old has content
NEW_CONTENT=$(ls -A "$NEW_DIR" 2>/dev/null || true)
NEW_EMPTY=$([[ -z "$NEW_CONTENT" ]] && echo "true" || echo "false")

if [[ "$NEW_EMPTY" == "true" ]]; then
    echo "Moving workspace to contextspace (overriding empty contextspace)..."
    echo "Files to move:"
    ls -la "$OLD_DIR"
    if [[ -z "$DRY_RUN" ]]; then
        mv "$OLD_DIR"/* "$NEW_DIR"/
        rmdir "$OLD_DIR"
    fi
    echo "Done: moved workspace to contextspace"
    exit 0
fi

# Case 5: both dirs are non-empty - ERROR
cat <<EOF >&2
Error: Both workspace and contextspace directories exist and contain files.

Refusing to migrate to avoid data loss. Please resolve manually:

  Old directory: $OLD_DIR
  New directory: $NEW_DIR

Options:
  1. If contextspace is the source of truth, remove workspace manually:
     rm -rf "$OLD_DIR"

  2. If workspace has important files, move them to contextspace manually:
     mv "$OLD_DIR"/* "$NEW_DIR"/
     rmdir "$OLD_DIR"

  3. If you want to inspect both directories first:
     ls -la "$OLD_DIR"
     ls -la "$NEW_DIR"
EOF
exit 1
