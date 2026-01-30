"""Portable ticket management CLI (self-contained, no repo imports)."""

from __future__ import annotations

from pathlib import Path

MANAGER_BASENAME = "ticket_tool.py"
MANAGER_REL_PATH = Path(".codex-autorunner/bin") / MANAGER_BASENAME

_SCRIPT = """#!/usr/bin/env python3
\"\"\"Manage Codex Autorunner tickets (list, insert, move, create, lint).

Commands:
  list                   Show ticket order with titles/done flags.
  lint                   Validate ticket filenames and frontmatter.
  insert --before N      Shift tickets >= N up by COUNT (default 1).
  insert --after N       Shift tickets > N up by COUNT (default 1).
  move --start A --to B  Move ticket/block starting at A (or A..END)
                         so it begins at position B (1-indexed).
  create --title \"...\"   Create a new ticket at the next or specified
                         index. Use --at to place into a gap.

Examples:
  ticket_tool.py list
  ticket_tool.py insert --before 3
  ticket_tool.py create --title \"Investigate flaky test\" --at 3
  ticket_tool.py move --start 5 --end 7 --to 2
  ticket_tool.py lint

Notes:
- Filenames must match TICKET-<number>[suffix].md.
- PyYAML is required (pip install pyyaml) for linting/title extraction.
- The tool is intentionally dependency-light and safe to run from any
  virtualenv (or none).
\"\"\"

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

_TICKET_NAME_RE = re.compile(r"^TICKET-(\\d{3,})([^/]*)\\.md$", re.IGNORECASE)


@dataclass
class TicketFile:
    index: int
    path: Path
    suffix: str
    title: Optional[str]
    done: Optional[bool]


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "tickets"


def _ticket_paths(ticket_dir: Path) -> Tuple[List[Path], List[str]]:
    tickets: List[tuple[int, Path, str]] = []
    errors: List[str] = []
    for path in sorted(ticket_dir.iterdir()):
        if not path.is_file():
            continue
        m = _TICKET_NAME_RE.match(path.name)
        if not m:
            errors.append(
                f\"{path}: Invalid ticket filename; expected TICKET-<number>[suffix].md\"
            )
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            errors.append(f\"{path}: Invalid ticket filename; number must be digits\")
            continue
        tickets.append((idx, path, m.group(2)))
    tickets.sort(key=lambda t: t[0])
    return [p for _, p, _ in tickets], errors


def _split_frontmatter(text: str):
    if not text or not text.lstrip().startswith(\"---\"):
        return None, [\"Missing YAML frontmatter (expected leading '---').\"]
    lines = text.splitlines()
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in (\"---\", \"...\"):
            end_idx = idx
            break
    if end_idx is None:
        return None, [\"Frontmatter is not closed (missing trailing '---').\"]
    fm_yaml = \"\\n\".join(lines[1:end_idx])
    return fm_yaml, []


def _parse_yaml(fm_yaml: Optional[str]):
    if fm_yaml is None:
        return {}, [\"Missing or invalid YAML frontmatter (expected a mapping).\"]
    if yaml is None:
        return {}, [
            \"PyYAML is required to lint tickets. Install with: python3 -m pip install --user pyyaml\"
        ]
    try:
        loaded = yaml.safe_load(fm_yaml)
    except Exception as exc:  # noqa: BLE001
        return {}, [f\"YAML parse error: {exc}\"]
    if loaded is None or not isinstance(loaded, dict):
        return {}, [\"Invalid YAML frontmatter (expected a mapping).\"]
    return loaded, []


def _lint_frontmatter(data: dict):
    errors: List[str] = []
    agent = data.get(\"agent\")
    if not isinstance(agent, str) or not agent.strip():
        errors.append(\"frontmatter.agent is required and must be a non-empty string.\")
    done = data.get(\"done\")
    if not isinstance(done, bool):
        errors.append(\"frontmatter.done is required and must be a boolean.\")
    return errors


def _read_ticket(path: Path) -> Tuple[Optional[TicketFile], List[str]]:
    try:
        raw = path.read_text(encoding=\"utf-8\")
    except OSError as exc:
        return None, [f\"{path}: Unable to read file ({exc}).\"]

    fm_yaml, fm_errors = _split_frontmatter(raw)
    if fm_errors:
        return None, [f\"{path}: {msg}\" for msg in fm_errors]

    data, parse_errors = _parse_yaml(fm_yaml)
    if parse_errors:
        return None, [f\"{path}: {msg}\" for msg in parse_errors]

    lint_errors = _lint_frontmatter(data)
    if lint_errors:
        return None, [f\"{path}: {msg}\" for msg in lint_errors]

    title = data.get(\"title\") if isinstance(data, dict) else None
    done_val = data.get(\"done\") if isinstance(data, dict) else None

    m = _TICKET_NAME_RE.match(path.name)
    idx = int(m.group(1)) if m else 0
    suffix = m.group(2) if m else \"\"
    return TicketFile(index=idx, path=path, suffix=suffix, title=title, done=done_val), []


def _ticket_files(ticket_dir: Path) -> Tuple[List[TicketFile], List[str]]:
    paths, name_errors = _ticket_paths(ticket_dir)
    tickets: List[TicketFile] = []
    errors = list(name_errors)
    for path in paths:
        ticket, errs = _read_ticket(path)
        if ticket:
            tickets.append(ticket)
        errors.extend(errs)
    tickets.sort(key=lambda t: t.index)
    return tickets, errors


def _pad_width(indices: Sequence[int]) -> int:
    if not indices:
        return 3
    return max(3, max(len(str(i)) for i in indices))


def _fmt_name(index: int, suffix: str, width: int) -> str:
    return f\"TICKET-{index:0{width}d}{suffix}.md\"


def _safe_renames(mapping: Sequence[tuple[Path, Path]]) -> None:
    temp_pairs: list[tuple[Path, Path]] = []
    for src, dst in mapping:
        if src == dst:
            continue
        temp = src.with_name(src.name + \".tmp-move\")
        counter = 0
        while temp.exists():
            counter += 1
            temp = src.with_name(f\"{src.name}.tmp-move-{counter}\")
        src.rename(temp)
        temp_pairs.append((temp, dst))

    for temp, dst in temp_pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        temp.rename(dst)


def cmd_list(ticket_dir: Path) -> int:
    tickets, errors = _ticket_files(ticket_dir)
    if errors:
        for msg in errors:
            sys.stderr.write(msg + \"\\n\")
    width = _pad_width([t.index for t in tickets])
    for t in tickets:
        status = \"done\" if t.done else \"open\"
        title = f\" - {t.title}\" if t.title else \"\"
        sys.stdout.write(f\"{t.index:0{width}d} [{status}] {t.path.name}{title}\\n\")
    if errors:
        return 1
    return 0


def cmd_lint(ticket_dir: Path) -> int:
    paths, name_errors = _ticket_paths(ticket_dir)
    errors = list(name_errors)
    for path in paths:
        _, errs = _read_ticket(path)
        errors.extend(errs)

    if errors:
        for msg in errors:
            sys.stderr.write(msg + \"\\n\")
        return 1
    sys.stdout.write(f\"OK: {len(paths)} ticket(s) linted.\\n\")
    return 0


def _shift(ticket_dir: Path, start_idx: int, delta: int) -> None:
    if delta == 0:
        return
    paths, errors = _ticket_paths(ticket_dir)
    if errors:
        raise ValueError(\"Cannot shift while filenames are invalid; run lint first.\")
    iterable = reversed(paths) if delta > 0 else paths
    width = _pad_width([_parse_index(p.name) for p in paths] + [start_idx + delta])
    mapping: list[tuple[Path, Path]] = []
    for path in iterable:
        idx = _parse_index(path.name)
        if idx is None or idx < start_idx:
            continue
        new_idx = idx + delta
        if new_idx <= 0:
            raise ValueError(\"Shift would create non-positive ticket index\")
        suffix = _parse_suffix(path.name)
        target = path.with_name(_fmt_name(new_idx, suffix, width))
        mapping.append((path, target))
    _safe_renames(mapping)


def _parse_index(name: str) -> Optional[int]:
    m = _TICKET_NAME_RE.match(name)
    return int(m.group(1)) if m else None


def _parse_suffix(name: str) -> str:
    m = _TICKET_NAME_RE.match(name)
    return m.group(2) if m else \"\"


def cmd_insert(ticket_dir: Path, *, before: Optional[int], after: Optional[int], count: int) -> int:
    if (before is None) == (after is None):
        sys.stderr.write(\"Specify exactly one of --before or --after.\\n\")
        return 2
    anchor = before if before is not None else after + 1  # type: ignore[operator]
    if anchor is None or anchor < 1:
        sys.stderr.write(\"Anchor index must be >= 1.\\n\")
        return 2
    try:
        _shift(ticket_dir, anchor, count)
    except ValueError as exc:
        sys.stderr.write(str(exc) + \"\\n\")
        return 1
    return 0


def cmd_create(ticket_dir: Path, *, title: str, agent: str, at: Optional[int]) -> int:
    tickets, errors = _ticket_files(ticket_dir)
    if errors:
        for msg in errors:
            sys.stderr.write(msg + \"\\n\")
        return 1
    existing_indices = [t.index for t in tickets]
    next_index = max(existing_indices) + 1 if existing_indices else 1
    index = at or next_index
    if index in existing_indices:
        sys.stderr.write(
            f\"Ticket index {index} already exists. Use insert to open a gap or choose --at another index.\\n\"
        )
        return 1
    width = _pad_width(existing_indices + [index])
    name = _fmt_name(index, \"\", width)
    path = ticket_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f\"---\\n\"
        f\"title: {title}\\n\"
        f\"agent: {agent}\\n\"
        f\"done: false\\n\"
        f\"---\\n\\n\"
        f\"## Goal\\n- \\n\"
    )
    path.write_text(body, encoding=\"utf-8\")
    sys.stdout.write(f\"Created {path}\\n\")
    return 0


def cmd_move(ticket_dir: Path, *, start: int, end: Optional[int], to: int) -> int:
    if start < 1 or to < 1:
        sys.stderr.write(\"Indices must be >= 1.\\n\")
        return 2
    tickets, errors = _ticket_files(ticket_dir)
    if errors:
        for msg in errors:
            sys.stderr.write(msg + \"\\n\")
        return 1
    indices = [t.index for t in tickets]
    if start not in indices:
        sys.stderr.write(f\"No ticket at index {start}.\\n\")
        return 1
    end_idx = end if end is not None else start
    if end_idx < start:
        sys.stderr.write(\"--end must be >= --start.\\n\")
        return 2
    block = [t for t in tickets if start <= t.index <= end_idx]
    if not block:
        sys.stderr.write(\"No tickets in the specified move range.\\n\")
        return 1
    remaining = [t for t in tickets if t not in block]
    insert_pos = to - 1
    if insert_pos < 0 or insert_pos > len(remaining):
        sys.stderr.write(\"Target position is out of range.\\n\")
        return 1
    new_order = remaining[:insert_pos] + block + remaining[insert_pos:]
    width = _pad_width([t.index for t in new_order])

    mapping: list[tuple[Path, Path]] = []
    for new_idx, ticket in enumerate(new_order, start=1):
        target = ticket.path.with_name(_fmt_name(new_idx, ticket.suffix, width))
        mapping.append((ticket.path, target))
    _safe_renames(mapping)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=\"Manage Codex Autorunner tickets.\")
    sub = parser.add_subparsers(dest=\"cmd\", required=True)

    sub.add_parser(\"list\", help=\"List tickets in order\")
    sub.add_parser(\"lint\", help=\"Validate ticket filenames and frontmatter\")

    insert_p = sub.add_parser(\"insert\", help=\"Insert gap by shifting tickets\")
    insert_group = insert_p.add_mutually_exclusive_group(required=True)
    insert_group.add_argument(\"--before\", type=int, help=\"First index to shift upward\")
    insert_group.add_argument(\"--after\", type=int, help=\"Shift tickets after this index\")
    insert_p.add_argument(\"--count\", type=int, default=1, help=\"How many slots to insert (default 1)\")

    create_p = sub.add_parser(\"create\", help=\"Create a new ticket\")
    create_p.add_argument(\"--title\", required=True, help=\"Ticket title\")
    create_p.add_argument(\"--agent\", default=\"codex\", help=\"Frontmatter agent (default: codex)\")
    create_p.add_argument(
        \"--at\",
        type=int,
        help=\"Index to use (must be unused). Defaults to next available index.\",
    )

    move_p = sub.add_parser(\"move\", help=\"Move a ticket or block to a new position\")
    move_p.add_argument(\"--start\", type=int, required=True, help=\"First index in the block to move\")
    move_p.add_argument(\"--end\", type=int, help=\"Last index in the block (defaults to start)\")
    move_p.add_argument(\"--to\", type=int, required=True, help=\"Destination position (1-indexed)\")

    args = parser.parse_args(argv)
    repo_root = Path.cwd()
    ticket_dir = _ticket_dir(repo_root)
    if not ticket_dir.exists():
        sys.stderr.write(f\"Tickets directory not found: {ticket_dir}\\n\")
        return 2

    if args.cmd == \"list\":
        return cmd_list(ticket_dir)
    if args.cmd == \"lint\":
        return cmd_lint(ticket_dir)
    if args.cmd == \"insert\":
        return cmd_insert(ticket_dir, before=args.before, after=args.after, count=args.count)
    if args.cmd == \"create\":
        return cmd_create(ticket_dir, title=args.title, agent=args.agent, at=args.at)
    if args.cmd == \"move\":
        return cmd_move(ticket_dir, start=args.start, end=args.end, to=args.to)
    parser.error(\"Unknown command\")
    return 2


if __name__ == \"__main__\":  # pragma: no cover
    sys.exit(main())
"""


def ensure_ticket_manager(repo_root: Path, *, force: bool = False) -> Path:
    """Ensure the ticket management CLI exists under .codex-autorunner/bin."""

    path = repo_root / MANAGER_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None

    if force or existing != _SCRIPT:
        path.write_text(_SCRIPT, encoding="utf-8")
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)

    return path
