from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional, cast

from ....core.flows import FlowEventType
from ....tickets.files import read_ticket, safe_relpath
from ....tickets.frontmatter import (
    deterministic_ticket_id,
    parse_markdown_frontmatter,
    sanitize_ticket_id,
    split_markdown_frontmatter,
)
from ....tickets.lint import parse_ticket_index


def ticket_status(frontmatter: dict[str, object] | None, errors: list[str]) -> str:
    if errors:
        return "invalid"
    if bool((frontmatter or {}).get("done")):
        return "done"
    return "idle"


def ticket_number_sort_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def string_values(*values: object) -> set[str]:
    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            result.add(text)
            name = Path(text).name
            result.add(name)
            if name.endswith(".md"):
                result.add(name[:-3])
    return {value.lower() for value in result if value}


def ticket_aliases(payload: dict[str, object]) -> set[str]:
    frontmatter = payload.get("frontmatter")
    fm = frontmatter if isinstance(frontmatter, dict) else {}
    aliases = string_values(
        payload.get("id"),
        payload.get("ticket_id"),
        payload.get("source_ticket_id"),
        payload.get("path"),
        payload.get("ticket_path"),
        fm.get("ticket_id"),
    )
    ticket_number = payload.get("ticket_number") or payload.get("index")
    if isinstance(ticket_number, int):
        aliases.update({str(ticket_number), f"ticket-{ticket_number:03d}".lower()})
    return aliases


def run_ticket_aliases(run_state: object, record: object) -> set[str]:
    state = run_state if isinstance(run_state, dict) else {}
    ticket_engine = state.get("ticket_engine")
    engine = ticket_engine if isinstance(ticket_engine, dict) else {}
    record_state = getattr(record, "state", None)
    raw_record_state = record_state if isinstance(record_state, dict) else {}
    raw_record_engine = raw_record_state.get("ticket_engine")
    record_engine = raw_record_engine if isinstance(raw_record_engine, dict) else {}
    return string_values(
        state.get("current_ticket"),
        state.get("current_ticket_id"),
        state.get("effective_current_ticket"),
        engine.get("current_ticket"),
        engine.get("current_ticket_id"),
        raw_record_state.get("current_ticket"),
        raw_record_state.get("current_ticket_id"),
        record_engine.get("current_ticket"),
        record_engine.get("current_ticket_id"),
    )


def run_diff_stats(store: object, run_id: str) -> Optional[dict[str, int]]:
    get_events = getattr(store, "get_events_by_type", None)
    if get_events is None:
        return None
    totals = {"insertions": 0, "deletions": 0, "files_changed": 0}
    try:
        events = get_events(run_id, FlowEventType.DIFF_UPDATED)
    except Exception:
        return None
    for event in events:
        data = getattr(event, "data", None) or {}
        if not isinstance(data, dict):
            continue
        totals["insertions"] += int(data.get("insertions") or 0)
        totals["deletions"] += int(data.get("deletions") or 0)
        totals["files_changed"] += int(data.get("files_changed") or 0)
    return totals if any(totals.values()) else None


def enrich_current_ticket_payload(
    payload: dict[str, object],
    *,
    run_state: object,
    run_record: object,
) -> None:
    from ....core.flows.models import FlowRunRecord, flow_run_duration_seconds

    if not run_record:
        return
    if not (ticket_aliases(payload) & run_ticket_aliases(run_state, run_record)):
        return
    payload["run_id"] = getattr(run_record, "id", None)
    payload["duration_seconds"] = flow_run_duration_seconds(
        cast(FlowRunRecord, run_record)
    )
    flow_status = (
        run_state.get("flow_status")
        if isinstance(run_state, dict)
        else getattr(run_record, "status", None)
    )
    if isinstance(flow_status, str) and flow_status.strip():
        payload["status"] = flow_status


def mark_duplicate_ticket_numbers(payloads: list[dict[str, object]]) -> None:
    by_number: dict[int, list[dict[str, object]]] = {}
    for payload in payloads:
        number = payload.get("ticket_number")
        if not isinstance(number, int):
            continue
        by_number.setdefault(number, []).append(payload)

    for number, duplicates in by_number.items():
        if len(duplicates) < 2:
            continue
        paths = ", ".join(
            str(item.get("ticket_path") or item.get("path")) for item in duplicates
        )
        message = (
            f"Duplicate ticket index {number:03d}: multiple files share the same index "
            f"({paths}). Rename or remove duplicates to ensure deterministic ordering."
        )
        for payload in duplicates:
            errors = payload.get("errors")
            if not isinstance(errors, list):
                errors = []
                payload["errors"] = errors
            errors.append(message)
            payload["status"] = ticket_status(None, [message])


def ticket_payload(
    *,
    hub_root: Path,
    workspace_root: Path,
    ticket_dir: Path,
    workspace_kind: str,
    workspace_id: str,
    repo_id: Optional[str],
    worktree_id: Optional[str],
    path: Path,
) -> dict[str, object]:
    resolved_path = path.resolve()
    try:
        if not resolved_path.is_relative_to(ticket_dir.resolve()):
            return {}
    except ValueError:
        return {}
    doc, errors = read_ticket(path)
    idx = getattr(doc, "index", None) or parse_ticket_index(path.name)
    parsed_frontmatter: dict[str, object] = {}
    parsed_body: str | None = None
    raw_frontmatter_yaml: str | None = None
    if doc is None:
        try:
            raw_body = path.read_text(encoding="utf-8")
            raw_frontmatter_yaml, _ = split_markdown_frontmatter(raw_body)
            parsed_frontmatter, parsed_body = parse_markdown_frontmatter(raw_body)
        except (OSError, ValueError):
            parsed_frontmatter, parsed_body, raw_frontmatter_yaml = {}, None, None
    else:
        try:
            raw_body = path.read_text(encoding="utf-8")
            raw_frontmatter_yaml, _ = split_markdown_frontmatter(raw_body)
        except OSError:
            raw_frontmatter_yaml = None

    frontmatter = asdict(doc.frontmatter) if doc else parsed_frontmatter
    source_ticket_id = sanitize_ticket_id(
        frontmatter.get("ticket_id")
    ) or deterministic_ticket_id(path)
    ticket_path = safe_relpath(path, workspace_root)
    global_ticket_id = f"{workspace_kind}:{workspace_id}:{source_ticket_id}"
    try:
        workspace_path = str(workspace_root.relative_to(hub_root))
    except ValueError:
        workspace_path = str(workspace_root)

    return {
        "id": global_ticket_id,
        "ticket_id": source_ticket_id,
        "source_ticket_id": source_ticket_id,
        "path": ticket_path,
        "ticket_path": ticket_path,
        "index": idx,
        "ticket_number": idx,
        "chat_key": f"ticket:{idx}:{source_ticket_id}" if idx else None,
        "frontmatter": frontmatter,
        "frontmatter_yaml": raw_frontmatter_yaml,
        "body": doc.body if doc else parsed_body,
        "errors": errors,
        "status": ticket_status(frontmatter, errors),
        "workspace_kind": workspace_kind,
        "workspace_id": workspace_id,
        "workspace_path": workspace_path,
        "hub_root": str(hub_root),
        "workspace_root": str(workspace_root),
        "repo_id": repo_id,
        "worktree_id": worktree_id,
        "base_repo_id": repo_id if workspace_kind == "worktree" else None,
    }


__all__ = [
    "enrich_current_ticket_payload",
    "mark_duplicate_ticket_numbers",
    "run_diff_stats",
    "run_ticket_aliases",
    "ticket_aliases",
    "ticket_number_sort_value",
    "ticket_payload",
    "ticket_status",
]
