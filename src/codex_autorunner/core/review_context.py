from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from .flows.store import FlowStore
from .utils import is_within

if TYPE_CHECKING:
    from .runtime import RuntimeContext


TRUNCATION_SUFFIX = "... (truncated)\n"


def _truncate_text(text: str, limit: Optional[int]) -> str:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    head = text[: max(0, limit - len(TRUNCATION_SUFFIX))]
    return head.rstrip() + TRUNCATION_SUFFIX


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"(failed to read {path.name}: {exc})"


def _artifact_entries(
    ctx: "RuntimeContext", run_id: Optional[int], max_doc_chars: Optional[int]
) -> list[tuple[str, str]]:
    db_path = ctx.state_root / "flows.db"
    if not db_path.exists():
        return []

    try:
        with FlowStore(db_path, durable=ctx.config.durable_writes) as store:
            records = store.list_flow_runs(flow_type="ticket_flow")
            if not records:
                records = store.list_flow_runs()
            if not records:
                return []
            selected = records[0]
            if run_id is not None:
                # Compatibility hook: if callers still pass a numeric legacy run id,
                # allow it to select a flow run when ids happen to match.
                override = store.get_flow_run(str(run_id))
                if override is not None:
                    selected = override
            artifacts = store.get_artifacts(selected.id)
            events = store.get_events(selected.id, limit=120)
    except Exception:
        return []

    repo_root = ctx.repo_root
    limit = (
        max_doc_chars if isinstance(max_doc_chars, int) and max_doc_chars > 0 else 4000
    )
    limit = max(2000, min(limit, 8000))
    pairs: list[tuple[str, str]] = []

    lines: list[str] = [
        f"- run_id: {selected.id}",
        f"- flow_type: {selected.flow_type}",
        f"- status: {selected.status.value}",
    ]
    if events:
        lines.append("- recent_events:")
        for event in events[-30:]:
            payload = event.data if isinstance(event.data, dict) else {}
            details = ""
            if payload:
                keys = [
                    "current_ticket",
                    "message",
                    "reason",
                    "status",
                    "error",
                    "step",
                ]
                compact = {
                    key: payload.get(key)
                    for key in keys
                    if key in payload and payload.get(key) is not None
                }
                if compact:
                    details = f" {json.dumps(compact, ensure_ascii=True)}"
            lines.append(
                f"  - #{event.seq} {event.timestamp} {event.event_type.value}{details}"
            )
    pairs.append(("Flow run summary", _truncate_text("\n".join(lines), limit)))

    label_by_kind = {
        "output": "Output",
        "diff": "Diff",
        "plan": "Plan",
    }
    for artifact in artifacts:
        raw = artifact.path if isinstance(artifact.path, str) else ""
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        if not is_within(repo_root, path) or not path.exists():
            continue
        label = label_by_kind.get(artifact.kind, f"Artifact ({artifact.kind})")
        content = _truncate_text(_safe_read(path), limit)
        pairs.append((label, content))
    return pairs


def build_spec_progress_review_context(
    ctx: "RuntimeContext",
    *,
    exit_reason: str,
    last_run_id: Optional[int],
    last_exit_code: Optional[int],
    max_doc_chars: int,
    primary_docs: Iterable[str],
    include_docs: Iterable[str],
    include_last_run_artifacts: bool,
) -> str:
    remaining = (
        max_doc_chars if isinstance(max_doc_chars, int) and max_doc_chars > 0 else None
    )
    parts: list[str] = []

    def add(text: str, *, annotate: bool = False) -> None:
        nonlocal remaining
        if text is None:
            return
        if remaining is None:
            parts.append(text)
            return
        if remaining <= 0:
            return
        if len(text) <= remaining:
            parts.append(text)
            remaining -= len(text)
            return
        if annotate and remaining > len(TRUNCATION_SUFFIX):
            snippet = text[: remaining - len(TRUNCATION_SUFFIX)]
            parts.append(snippet.rstrip() + TRUNCATION_SUFFIX)
        else:
            parts.append(text[:remaining])
        remaining = 0

    def doc_label(name: str) -> str:
        try:
            return ctx.config.doc_path(name).relative_to(ctx.repo_root).as_posix()
        except Exception:
            return name

    def read_doc(name: str) -> str:
        try:
            path = ctx.config.doc_path(name)
            return _safe_read(path)
        except Exception as exc:
            return f"(failed to read {name}: {exc})"

    add("# Autorunner Review Context\n\n")
    add("## Exit reason\n")
    add(f"- reason: {exit_reason or 'unknown'}\n")
    if last_run_id is not None:
        add(f"- last_run_id: {last_run_id}\n")
    if last_exit_code is not None:
        add(f"- last_exit_code: {last_exit_code}\n")
    add("\n")

    primary_list = [doc for doc in primary_docs if isinstance(doc, str)] or [
        "spec",
        "active_context",
    ]
    primary_set = {doc.lower() for doc in primary_list}

    add("## Primary docs\n")
    for key in primary_list:
        add(f"### {doc_label(key)}\n")
        content = read_doc(key).strip()
        add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    extras_seen = set()
    extra_docs = [doc for doc in include_docs if isinstance(doc, str)]
    if extra_docs:
        add("## Optional docs\n")
        for key in extra_docs:
            normalized = key.lower()
            if normalized in extras_seen or normalized in primary_set:
                continue
            extras_seen.add(normalized)
            add(f"### {doc_label(normalized)}\n")
            content = read_doc(normalized).strip()
            add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    if include_last_run_artifacts:
        if remaining is not None and remaining <= 0:
            return "".join(parts)
        add("## Last run artifacts\n")
        artifacts = _artifact_entries(
            ctx,
            last_run_id,
            remaining if remaining is not None else max_doc_chars,
        )
        if not artifacts:
            add("_No artifacts found_\n\n")
        else:
            for label, content in artifacts:
                add(f"### {label}\n")
                add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    return "".join(parts)


# Keep a source-level reference so dead-code heuristics do not misclassify this
# public helper that is primarily exercised via tests and external workflows.
_BUILD_SPEC_PROGRESS_REVIEW_CONTEXT = build_spec_progress_review_context
