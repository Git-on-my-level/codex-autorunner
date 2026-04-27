from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, TextIO

yaml_module: Any
try:
    import yaml as yaml_module
except ImportError:  # pragma: no cover
    yaml_module = None

yaml = yaml_module

_TICKET_NAME_RE = re.compile(r"^TICKET-(\d{3,})(?:[^/]*)\.md$", re.IGNORECASE)
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,128}$")
_KNOWN_AGENT_IDS = ("codex", "hermes", "opencode", "user", "zeroclaw")
_IGNORED_NON_TICKET_FILENAMES = {"AGENTS.md", "ingest_state.json"}


def _parse_scalar(raw: str) -> object:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return (
            value[1:-1].replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        )
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    if ": " in value or value.endswith(":"):
        raise ValueError("unsupported unquoted ':' in scalar")
    return value


def _parse_simple_yaml_mapping(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if line[:1].isspace():
            raise ValueError("unexpected indentation")
        if ":" not in line:
            raise ValueError("expected 'key: value'")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("missing mapping key")

        value = raw_value.strip()
        if value:
            data[key] = _parse_scalar(value)
            idx += 1
            continue

        idx += 1
        block: list[str] = []
        while idx < len(lines):
            child = lines[idx]
            if not child.strip():
                idx += 1
                continue
            if not child.startswith("  "):
                break
            block.append(child[2:])
            idx += 1

        if not block:
            data[key] = None
            continue

        if all(item.lstrip().startswith("- ") for item in block):
            values: list[Any] = []
            for item in block:
                stripped = item.lstrip()
                if not stripped.startswith("- "):
                    raise ValueError("mixed list indentation")
                values.append(_parse_scalar(stripped[2:].strip()))
            data[key] = values
            continue

        data[key] = _parse_simple_yaml_mapping("\n".join(block))

    return data


def _dump_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    needs_quotes = (
        not text
        or text != text.strip()
        or ": " in text
        or text.endswith(":")
        or "\n" in text
        or '"' in text
        or "\\" in text
    )
    if not needs_quotes:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _dump_simple_yaml(data: dict[str, Any], *, indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            nested = _dump_simple_yaml(value, indent=indent + 2)
            if nested:
                lines.append(nested)
            continue
        if isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            for item in value:
                lines.append(f"{prefix}  - {_dump_scalar(item)}")
            continue
        lines.append(f"{prefix}{key}: {_dump_scalar(value)}")
    return "\n".join(lines)


def _sanitize_ticket_id(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or not _TICKET_ID_RE.match(cleaned):
        return None
    return cleaned


def _normalize_agent(raw: object) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(raw, str):
        return None, "frontmatter.agent is required (e.g. 'codex' or 'opencode')."

    cleaned = raw.strip()
    if not cleaned:
        return None, "frontmatter.agent is required (e.g. 'codex' or 'opencode')."

    normalized = cleaned.lower()
    if normalized not in _KNOWN_AGENT_IDS:
        return None, f"frontmatter.agent is invalid: Unknown agent: {cleaned!r}"

    return normalized, None


def _lint_frontmatter(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    raw_ticket_id = data.get("ticket_id")
    ticket_id = _sanitize_ticket_id(raw_ticket_id)
    if raw_ticket_id is not None and not ticket_id:
        errors.append(
            "frontmatter.ticket_id must match [A-Za-z0-9._-]{6,128} when provided."
        )

    _agent, agent_error = _normalize_agent(data.get("agent"))
    if agent_error:
        errors.append(agent_error)

    done = data.get("done")
    if not isinstance(done, bool):
        errors.append("frontmatter.done is required and must be a boolean.")

    return errors


def _ticket_paths(tickets_dir: Path) -> tuple[list[Path], list[str]]:
    tickets: list[tuple[int, Path]] = []
    errors: list[str] = []
    index_to_paths: dict[int, list[Path]] = {}
    for path in sorted(tickets_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name in _IGNORED_NON_TICKET_FILENAMES:
            continue
        match = _TICKET_NAME_RE.match(path.name)
        if not match:
            errors.append(
                f"{path}: Invalid ticket filename; expected TICKET-<number>[suffix].md (e.g. TICKET-001-foo.md)"
            )
            continue
        try:
            idx = int(match.group(1))
        except ValueError:
            errors.append(
                f"{path}: Invalid ticket filename; ticket number must be digits (e.g. 001)"
            )
            continue
        tickets.append((idx, path))
        index_to_paths.setdefault(idx, []).append(path)
    tickets.sort(key=lambda pair: pair[0])

    for idx, paths in index_to_paths.items():
        if len(paths) > 1:
            paths_str = ", ".join(str(path) for path in paths)
            errors.append(
                f"Duplicate ticket index {idx:03d}: multiple files share the same index ({paths_str}). "
                "Rename or remove duplicates to ensure deterministic ordering."
            )

    return [path for _, path in tickets], errors


def _split_frontmatter(text: str) -> tuple[Optional[str], list[str]]:
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


def _parse_yaml(fm_yaml: Optional[str]) -> tuple[dict[str, Any], list[str]]:
    if fm_yaml is None:
        return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]
    if yaml is None:
        try:
            loaded = _parse_simple_yaml_mapping(fm_yaml)
        except ValueError as exc:
            return {}, [f"YAML parse error: {exc}"]
        return loaded, []

    try:
        loaded = yaml.safe_load(fm_yaml)
    except yaml.YAMLError as exc:
        return {}, [f"YAML parse error: {exc}"]

    if loaded is None:
        return {}, ["Missing or invalid YAML frontmatter (expected a mapping)."]
    if not isinstance(loaded, dict):
        return {}, ["Invalid YAML frontmatter (expected a mapping)."]

    return loaded, []


def lint_ticket(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: Unable to read file ({exc})."]

    fm_yaml, fm_errors = _split_frontmatter(raw)
    if fm_errors:
        return [f"{path}: {msg}" for msg in fm_errors]

    data, parse_errors = _parse_yaml(fm_yaml)
    if parse_errors:
        return [f"{path}: {msg}" for msg in parse_errors]

    lint_errors = _lint_frontmatter(data)
    return [f"{path}: {msg}" for msg in lint_errors]


def _read_ticket_id(path: Path) -> Optional[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm_yaml, fm_errors = _split_frontmatter(raw)
    if fm_errors:
        return None

    data, parse_errors = _parse_yaml(fm_yaml)
    if parse_errors:
        return None

    return _sanitize_ticket_id(data.get("ticket_id"))


def _render_ticket(data: dict[str, Any], body: str) -> str:
    if yaml is None:
        fm_yaml = _dump_simple_yaml(data).rstrip()
    else:
        fm_yaml = yaml.safe_dump(data, sort_keys=False).rstrip()
    return f"---\n{fm_yaml}\n---\n{body}"


def _fix_ticket_id(path: Path) -> tuple[bool, list[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, [f"{path}: Unable to read file ({exc})."]

    fm_yaml, fm_errors = _split_frontmatter(raw)
    if fm_errors:
        return False, [f"{path}: {msg}" for msg in fm_errors]

    data, parse_errors = _parse_yaml(fm_yaml)
    if parse_errors:
        return False, [f"{path}: {msg}" for msg in parse_errors]

    ticket_id = data.get("ticket_id")
    if isinstance(ticket_id, str) and _TICKET_ID_RE.match(ticket_id.strip()):
        return False, []

    lines = raw.splitlines()
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end_idx = idx
            break
    if end_idx is None:
        return False, [f"{path}: Frontmatter is not closed (missing trailing '---')."]

    body = "\n".join(lines[end_idx + 1 :])
    rendered = _render_ticket(
        {**data, "ticket_id": f"tkt_{uuid.uuid4().hex}"},
        body,
    )
    if rendered != raw:
        path.write_text(rendered, encoding="utf-8")
        return True, []
    return False, []


def run_ticket_lint(
    tickets_dir: Path,
    *,
    fix_ticket_ids: bool = False,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr

    if not tickets_dir.exists():
        err.write(
            f"Tickets directory not found: {tickets_dir}\n"
            "Run from a Codex Autorunner repo with .codex-autorunner/tickets present.\n"
        )
        return 2

    errors: list[str] = []
    ticket_paths, name_errors = _ticket_paths(tickets_dir)
    errors.extend(name_errors)

    fixed = 0
    if fix_ticket_ids:
        for path in ticket_paths:
            changed, fix_errors = _fix_ticket_id(path)
            if changed:
                fixed += 1
            errors.extend(fix_errors)

    for path in ticket_paths:
        errors.extend(lint_ticket(path))

    ticket_id_to_paths: dict[str, list[Path]] = {}
    for path in ticket_paths:
        ticket_id = _read_ticket_id(path)
        if not ticket_id:
            continue
        ticket_id_to_paths.setdefault(ticket_id, []).append(path)

    for ticket_id, paths in ticket_id_to_paths.items():
        if len(paths) > 1:
            paths_str = ", ".join(str(path) for path in paths)
            errors.append(
                f"Duplicate ticket_id {ticket_id!r}: multiple files share the same logical ticket identity ({paths_str}). "
                "Backfill or rewrite one of the ticket_ids so ticket-owned state remains unambiguous."
            )

    if not ticket_paths:
        if errors:
            for msg in errors:
                err.write(msg + "\n")
            return 1
        err.write(f"No tickets found in {tickets_dir}\n")
        return 1

    if errors:
        for msg in errors:
            err.write(msg + "\n")
        return 1

    if fixed:
        out.write(f"Backfilled ticket_id in {fixed} ticket(s).\n")
    out.write(f"OK: {len(ticket_paths)} ticket(s) linted.\n")
    return 0
