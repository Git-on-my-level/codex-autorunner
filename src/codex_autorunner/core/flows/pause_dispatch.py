from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import ConfigError, load_repo_config
from ..redaction import redact_text
from ..ticket_flow_projection import select_authoritative_run_record
from .models import FlowRunRecord, FlowRunStatus
from .store import FlowStore


def _get_durable_writes(repo_root: Path) -> bool:
    """Get durable_writes from repo config, defaulting to False if uninitialized."""
    try:
        return load_repo_config(repo_root).durable_writes
    except ConfigError:
        return False


@dataclass(frozen=True)
class PauseDispatchSnapshot:
    run_id: str
    dispatch_seq: str
    dispatch_markdown: str
    dispatch_dir: Optional[Path]
    allow_resume_hint: bool = True


@dataclass(frozen=True)
class TicketFlowDispatchSnapshot:
    run_id: str
    dispatch_seq: str
    dispatch_markdown: str
    dispatch_dir: Optional[Path]
    mode: str
    is_handoff: bool
    allow_resume_hint: bool = False


def latest_dispatch_seq(history_dir: Path) -> Optional[str]:
    if not history_dir.exists() or not history_dir.is_dir():
        return None
    seqs = [
        child.name
        for child in history_dir.iterdir()
        if child.is_dir() and not child.name.startswith(".") and child.name.isdigit()
    ]
    if not seqs:
        return None
    return max(seqs)


def _iter_dispatch_history_dirs(history_dir: Path) -> list[Path]:
    if not history_dir.exists() or not history_dir.is_dir():
        return []
    seq_dirs = [
        child
        for child in history_dir.iterdir()
        if child.is_dir() and not child.name.startswith(".") and child.name.isdigit()
    ]
    return sorted(seq_dirs, key=lambda path: path.name, reverse=True)


def _format_public_error(detail: str, *, limit: int = 200) -> str:
    normalized = " ".join(detail.split())
    redacted = redact_text(normalized)
    if len(redacted) > limit:
        return f"{redacted[: limit - 3]}..."
    return redacted


def format_pause_reason(record: FlowRunRecord) -> str:
    state = record.state or {}
    engine = state.get("ticket_engine") or {}
    reason_raw = (
        engine.get("reason") or record.error_message or "Paused without details."
    )
    reason = (
        _format_public_error(str(reason_raw))
        if reason_raw
        else "Paused without details."
    )
    reason_details_raw = engine.get("reason_details")
    if isinstance(reason_details_raw, str) and reason_details_raw.strip():
        details = _format_public_error(reason_details_raw, limit=500)
        return f"Reason: {reason}\n\nDetails: {details}"
    return f"Reason: {reason}"


def _render_dispatch_for_chat(*, title: Optional[str], body: str) -> str:
    parts: list[str] = []
    title_text = title.strip() if isinstance(title, str) and title.strip() else ""
    body_text = body.strip()
    if title_text:
        parts.append(title_text)
    if body_text:
        parts.append(body_text)
    return "\n\n".join(parts).strip()


def _format_dispatch_parse_failure(*, seq: str, errors: list[str]) -> str:
    lines = [f"Latest paused dispatch #{seq} is unreadable or invalid."]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for error in errors[:5]:
            lines.append(f"- {_format_public_error(error, limit=300)}")
        if len(errors) > 5:
            lines.append(f"- ...and {len(errors) - 5} more")
    lines.append("")
    lines.append("Fix DISPATCH.md for that paused turn before resuming.")
    return "\n".join(lines)


def _format_live_dispatch_parse_failure(*, seq: str, errors: list[str]) -> str:
    lines = [f"Dispatch #{seq} is unreadable or invalid."]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for error in errors[:5]:
            lines.append(f"- {_format_public_error(error, limit=300)}")
        if len(errors) > 5:
            lines.append(f"- ...and {len(errors) - 5} more")
    lines.append("")
    lines.append("Fix DISPATCH.md for that turn before continuing.")
    return "\n".join(lines)


def list_unseen_ticket_flow_dispatches(
    workspace_root: Path,
    *,
    last_run_id: Optional[str] = None,
    last_dispatch_seq: Optional[str] = None,
    bootstrap_latest_only: bool = True,
) -> list[TicketFlowDispatchSnapshot]:
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return []

    with FlowStore(db_path, durable=_get_durable_writes(workspace_root)) as store:
        runs = store.list_flow_runs(flow_type="ticket_flow")
        latest = select_authoritative_run_record(runs)
        if latest is None:
            return []

    from ...tickets.outbox import parse_dispatch, resolve_outbox_paths

    paths = resolve_outbox_paths(workspace_root=workspace_root, run_id=latest.id)
    seq_dirs = list(reversed(_iter_dispatch_history_dirs(paths.dispatch_history_dir)))

    last_seen_seq: Optional[int] = None
    if last_run_id == latest.id and isinstance(last_dispatch_seq, str):
        raw_seq = last_dispatch_seq.strip()
        if raw_seq.isdigit():
            last_seen_seq = int(raw_seq)

    run_is_paused = latest.status == FlowRunStatus.PAUSED
    if not seq_dirs:
        if not run_is_paused:
            return []
        if last_run_id == latest.id and last_dispatch_seq == "paused":
            return []
        return [
            TicketFlowDispatchSnapshot(
                run_id=latest.id,
                dispatch_seq="paused",
                dispatch_markdown=format_pause_reason(latest),
                dispatch_dir=None,
                mode="pause",
                is_handoff=True,
                allow_resume_hint=True,
            )
        ]

    unseen_seq_dirs = [
        dispatch_dir
        for dispatch_dir in seq_dirs
        if last_seen_seq is None or int(dispatch_dir.name) > last_seen_seq
    ]

    snapshots: list[TicketFlowDispatchSnapshot] = []
    saw_notification_worthy_unseen_history = False
    for dispatch_dir in unseen_seq_dirs:
        seq = dispatch_dir.name
        dispatch_path = dispatch_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        if errors or dispatch is None:
            saw_notification_worthy_unseen_history = True
            snapshots.append(
                TicketFlowDispatchSnapshot(
                    run_id=latest.id,
                    dispatch_seq=seq,
                    dispatch_markdown=(
                        _format_dispatch_parse_failure(seq=seq, errors=errors)
                        if run_is_paused
                        else _format_live_dispatch_parse_failure(
                            seq=seq,
                            errors=errors,
                        )
                    ),
                    dispatch_dir=dispatch_dir,
                    mode="pause" if run_is_paused else "notify",
                    is_handoff=run_is_paused,
                    allow_resume_hint=False,
                )
            )
            continue
        if dispatch.mode != "turn_summary":
            saw_notification_worthy_unseen_history = True
        if dispatch.mode == "turn_summary":
            continue
        snapshots.append(
            TicketFlowDispatchSnapshot(
                run_id=latest.id,
                dispatch_seq=seq,
                dispatch_markdown=_render_dispatch_for_chat(
                    title=dispatch.title,
                    body=dispatch.body,
                ),
                dispatch_dir=dispatch_dir,
                mode=dispatch.mode,
                is_handoff=dispatch.is_handoff or run_is_paused,
                allow_resume_hint=dispatch.is_handoff or run_is_paused,
            )
        )

    if (
        not snapshots
        and run_is_paused
        and last_seen_seq is None
        and unseen_seq_dirs
        and not saw_notification_worthy_unseen_history
    ):
        fallback_seq = unseen_seq_dirs[-1].name
        snapshots.append(
            TicketFlowDispatchSnapshot(
                run_id=latest.id,
                dispatch_seq=fallback_seq,
                dispatch_markdown=format_pause_reason(latest),
                dispatch_dir=None,
                mode="pause",
                is_handoff=True,
                allow_resume_hint=True,
            )
        )

    if last_seen_seq is not None:
        return snapshots
    if not snapshots:
        return []
    if bootstrap_latest_only:
        return [snapshots[-1]]
    return snapshots


def load_latest_paused_ticket_flow_dispatch(
    workspace_root: Path,
) -> Optional[PauseDispatchSnapshot]:
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None

    with FlowStore(db_path, durable=_get_durable_writes(workspace_root)) as store:
        runs = store.list_flow_runs(
            flow_type="ticket_flow", status=FlowRunStatus.PAUSED
        )
        if not runs:
            return None
        latest = runs[0]

    from ...tickets.outbox import resolve_outbox_paths

    paths = resolve_outbox_paths(workspace_root=workspace_root, run_id=latest.id)
    history_dir = paths.dispatch_history_dir
    seq_dirs = _iter_dispatch_history_dirs(history_dir)
    if not seq_dirs:
        return PauseDispatchSnapshot(
            run_id=latest.id,
            dispatch_seq="paused",
            dispatch_markdown=format_pause_reason(latest),
            dispatch_dir=None,
            allow_resume_hint=True,
        )

    from ...tickets.outbox import parse_dispatch

    handoff_snapshot: Optional[PauseDispatchSnapshot] = None
    non_summary_snapshot: Optional[PauseDispatchSnapshot] = None
    turn_summary_snapshot: Optional[PauseDispatchSnapshot] = None
    error_snapshot: Optional[PauseDispatchSnapshot] = None
    latest_seq = seq_dirs[0].name

    for dispatch_dir in seq_dirs:
        dispatch_path = dispatch_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        if errors or dispatch is None:
            failure_snapshot = PauseDispatchSnapshot(
                run_id=latest.id,
                dispatch_seq=dispatch_dir.name,
                dispatch_markdown=_format_dispatch_parse_failure(
                    seq=dispatch_dir.name,
                    errors=errors,
                ),
                dispatch_dir=dispatch_dir,
                allow_resume_hint=False,
            )
            if dispatch_dir.name == latest_seq:
                return failure_snapshot
            if error_snapshot is None:
                error_snapshot = failure_snapshot
            continue
        snapshot = PauseDispatchSnapshot(
            run_id=latest.id,
            dispatch_seq=dispatch_dir.name,
            dispatch_markdown=_render_dispatch_for_chat(
                title=dispatch.title,
                body=dispatch.body,
            ),
            dispatch_dir=dispatch_dir,
            allow_resume_hint=True,
        )
        if dispatch.is_handoff:
            handoff_snapshot = snapshot
            break
        if dispatch.mode != "turn_summary" and non_summary_snapshot is None:
            non_summary_snapshot = snapshot
        if dispatch.mode == "turn_summary" and turn_summary_snapshot is None:
            turn_summary_snapshot = snapshot

    selected = handoff_snapshot or non_summary_snapshot or turn_summary_snapshot
    if selected is not None:
        return selected
    if error_snapshot is not None:
        return error_snapshot

    return PauseDispatchSnapshot(
        run_id=latest.id,
        dispatch_seq=latest_seq,
        dispatch_markdown=format_pause_reason(latest),
        dispatch_dir=seq_dirs[0],
        allow_resume_hint=True,
    )
