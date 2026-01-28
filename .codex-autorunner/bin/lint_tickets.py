#!/usr/bin/env python3
"""
Portable ticket frontmatter linter.

Validates YAML frontmatter for each .codex-autorunner/tickets/TICKET-*.md file:
- Parses YAML (using PyYAML if available).
- Requires frontmatter.agent to be a non-empty string.
- Requires frontmatter.done to be a boolean.

Exits non-zero on any error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    sys.stderr.write(
        "PyYAML is required to lint tickets. Install with:\n"
        "  python3 -m pip install --user pyyaml\n"
    )
    sys.exit(2)


def _ticket_paths(tickets_dir: Path) -> Iterable[Path]:
    return sorted(tickets_dir.glob("TICKET-*.md"))


def _split_frontmatter(text: str) -> Tuple[Optional[str], List[str]]:
    if not text:
        return None, ["Empty file; missing YAML frontmatter."]

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, ["Missing YAML frontmatter (expected leading '---')."]

    end_idx: Optional[int] = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end_idx = idx
            break

    if end_idx is None:
        return None, ["Frontmatter is not closed (missing trailing '---')."]

    fm_yaml = "\n".join(lines[1:end_idx])
    return fm_yaml, []


def _parse_yaml(fm_yaml: Optional[str]) -> Tuple[dict[str, Any], List[str]]:
    if fm_yaml is None:
        return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]

    try:
        loaded = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        return {}, [f"YAML parse error: {exc}"]

    if loaded is None:
        return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]

    if not isinstance(loaded, dict):
        return {}, ["Invalid YAML frontmatter (expected a mapping)."]

    return loaded, []


def _lint_frontmatter(data: dict[str, Any]) -> List[str]:
    errors: List[str] = []

    agent = data.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        errors.append("frontmatter.agent is required and must be a non-empty string.")

    done = data.get("done")
    if not isinstance(done, bool):
        errors.append("frontmatter.done is required and must be a boolean.")

    return errors


def lint_ticket(path: Path) -> List[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return [f"{path}: Unable to read file ({exc})."]

    fm_yaml, fm_errors = _split_frontmatter(raw)
    if fm_errors:
        return [f"{path}: {msg}" for msg in fm_errors]

    data, parse_errors = _parse_yaml(fm_yaml)
    if parse_errors:
        return [f"{path}: {msg}" for msg in parse_errors]

    lint_errors = _lint_frontmatter(data)
    return [f"{path}: {msg}" for msg in lint_errors]


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    tickets_dir = script_dir.parent / "tickets"

    if not tickets_dir.exists():
        sys.stderr.write(
            f"Tickets directory not found: {tickets_dir}\n"
            "Run from a Codex Autorunner repo with .codex-autorunner/tickets present.\n"
        )
        return 2

    errors: List[str] = []
    checked = 0
    for path in _ticket_paths(tickets_dir):
        checked += 1
        errors.extend(lint_ticket(path))

    if not checked:
        sys.stderr.write(f"No tickets found in {tickets_dir}\n")
        return 1

    if errors:
        for msg in errors:
            sys.stderr.write(msg + "\n")
        return 1

    sys.stdout.write(f"OK: {checked} ticket(s) linted.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
