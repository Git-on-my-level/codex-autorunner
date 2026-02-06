#!/usr/bin/env python3

"""
Check for legacy TODO/SUMMARY pipeline code.

This script ensures no legacy TODO.md or SUMMARY.md references remain in the codebase.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Legacy strings that should not appear in the codebase
LEGACY_PATTERNS = [
    "TODO.md",  # Legacy TODO artifact
    "SUMMARY.md",  # Legacy SUMMARY artifact
    "PROGRESS.md",  # Legacy PROGRESS artifact (not used in ticket-flow)
    "OPINIONS.md",  # Legacy OPINIONS artifact (not used in ticket-flow)
    "parse_todos",  # Legacy function to parse TODO.md
    "validate_todo_markdown",  # Legacy function to validate TODO.md
    "docs.todos(",  # Legacy DocsManager method
    "docs.todos_done(",  # Legacy DocsManager method
    ".todos_done(",  # Legacy RunnerStateManager method (misleading naming)
    ".summary_finalized(",  # Legacy RunnerStateManager method
    "DEFAULT_PROMPT_TEMPLATE",  # Legacy prompt template
    "FINAL_SUMMARY_PROMPT_TEMPLATE",  # Legacy final summary template
    "check_docs.py",  # Legacy TODO.md checker script
]

# Paths to exclude from checks
EXCLUDE_PATTERNS = [
    ".git",
    "__pycache__",
    "*.pyc",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "*.egg-info",
    "tests",  # Tests may reference legacy code for testing purposes
    "docs/archive",  # Archived docs may reference old patterns
    ".codex-autorunner/archive",  # Archived worktree snapshots are historical artifacts
    ".codex-autorunner/runs",  # Run dispatch/history are historical artifacts
    ".codex-autorunner/contextspace/tickets-backup",  # Backup tickets are historical
    ".codex-autorunner/dispatch",  # Dispatch artifacts are historical
    ".codex-autorunner/tickets",  # Ticket files describe what was done and may reference legacy code
    ".codex-autorunner/config.yml",  # Generated runtime config may reference legacy paths
    ".codex-autorunner/github_context",  # Downloaded GH context may reference legacy docs
    "scripts/check_legacy_pipeline.py",  # Script itself contains legacy strings
    "src/codex_autorunner/flows/review/service.py",  # Review prompts mention legacy docs for context
]


def should_check_path(path: Path) -> bool:
    """Check if a path should be scanned."""
    rel_path = path.relative_to(REPO_ROOT)
    path_str = str(rel_path)

    for exclude in EXCLUDE_PATTERNS:
        if exclude in path_str or path.match(exclude):
            return False

    return True


def check_file(path: Path) -> list[tuple[int, str]]:
    """Check a file for legacy patterns."""
    issues = []

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return issues

    lines = content.splitlines()
    for line_num, line in enumerate(lines, start=1):
        for pattern in LEGACY_PATTERNS:
            if pattern in line:
                # Skip comment lines that are explaining what was removed
                if "legacy" in line.lower() and "removed" in line.lower():
                    continue
                issues.append((line_num, line.strip()))

    return issues


def main() -> int:
    """Run the legacy check."""
    all_issues = []

    # Scan Python files
    for py_file in REPO_ROOT.rglob("*.py"):
        if should_check_path(py_file):
            issues = check_file(py_file)
            if issues:
                rel_path = py_file.relative_to(REPO_ROOT)
                all_issues.append((rel_path, issues))

    # Scan shell scripts
    for sh_file in REPO_ROOT.rglob("*.sh"):
        if should_check_path(sh_file):
            issues = check_file(sh_file)
            if issues:
                rel_path = sh_file.relative_to(REPO_ROOT)
                all_issues.append((rel_path, issues))

    # Scan YAML config files
    for yaml_file in REPO_ROOT.rglob("*.yml"):
        if should_check_path(yaml_file):
            issues = check_file(yaml_file)
            if issues:
                rel_path = yaml_file.relative_to(REPO_ROOT)
                all_issues.append((rel_path, issues))

    # Scan Markdown docs (but not in tests or archive)
    for md_file in REPO_ROOT.rglob("*.md"):
        if should_check_path(md_file):
            issues = check_file(md_file)
            if issues:
                rel_path = md_file.relative_to(REPO_ROOT)
                all_issues.append((rel_path, issues))

    if all_issues:
        print("Legacy TODO/SUMMARY pipeline code detected!", file=sys.stderr)
        print(file=sys.stderr)

        for file_path, issues in sorted(all_issues):
            print(f"{file_path}:", file=sys.stderr)
            for line_num, line in issues:
                print(f"  Line {line_num}: {line[:80]}", file=sys.stderr)
            print(file=sys.stderr)

        print(
            "Please remove all legacy TODO/SUMMARY pipeline references.",
            file=sys.stderr,
        )
        return 1

    print("Legacy check passed: no legacy TODO/SUMMARY patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
