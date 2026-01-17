#!/usr/bin/env python3

from __future__ import annotations

import re
import sys
from pathlib import Path


TODO_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX])\]\s+\S")
TODO_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def validate_todo_markdown(content: str) -> list[str]:
    errors: list[str] = []
    if content is None:
        return ["TODO is missing"]
    lines = content.splitlines()
    meaningful = [
        line
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not meaningful:
        return []
    checkbox_lines = [line for line in meaningful if TODO_CHECKBOX_RE.match(line)]
    if not checkbox_lines:
        errors.append("TODO must contain at least one checkbox task line like `- [ ] ...`.")
    bullet_lines = [line for line in meaningful if TODO_BULLET_RE.match(line)]
    non_checkbox_bullets = [line for line in bullet_lines if not TODO_CHECKBOX_RE.match(line)]
    if non_checkbox_bullets:
        sample = non_checkbox_bullets[0].strip()
        errors.append(
            "TODO contains non-checkbox bullet(s); use `- [ ] ...` instead. "
            f"Example: `{sample}`"
        )
    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    todo_path = repo_root / ".codex-autorunner" / "TODO.md"
    if not todo_path.exists():
        # Work docs are often generated locally and may be gitignored.
        # Don't fail the general check suite if they're absent.
        return 0
    content = todo_path.read_text(encoding="utf-8")
    errors = validate_todo_markdown(content)
    if errors:
        print("Work docs check failed:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

