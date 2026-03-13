from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import ConfigError, load_repo_config
from ..redaction import redact_text
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

    runs_dir_raw = latest.input_data.get("runs_dir")
    runs_dir = (
        Path(runs_dir_raw)
        if isinstance(runs_dir_raw, str) and runs_dir_raw
        else Path(".codex-autorunner/runs")
    )

    from ...tickets.outbox import resolve_outbox_paths

    paths = resolve_outbox_paths(
        workspace_root=workspace_root, runs_dir=runs_dir, run_id=latest.id
    )
    history_dir = paths.dispatch_history_dir
    seq_dirs = _iter_dispatch_history_dirs(history_dir)
    if not seq_dirs:
        return PauseDispatchSnapshot(
            run_id=latest.id,
            dispatch_seq="paused",
            dispatch_markdown=format_pause_reason(latest),
            dispatch_dir=None,
        )

    from ...tickets.outbox import parse_dispatch

    handoff_snapshot: Optional[PauseDispatchSnapshot] = None
    non_summary_snapshot: Optional[PauseDispatchSnapshot] = None
    turn_summary_snapshot: Optional[PauseDispatchSnapshot] = None

    for dispatch_dir in seq_dirs:
        dispatch_path = dispatch_dir / "DISPATCH.md"
        dispatch, errors = parse_dispatch(dispatch_path)
        if errors or dispatch is None:
            continue
        snapshot = PauseDispatchSnapshot(
            run_id=latest.id,
            dispatch_seq=dispatch_dir.name,
            dispatch_markdown=_render_dispatch_for_chat(
                title=dispatch.title,
                body=dispatch.body,
            ),
            dispatch_dir=dispatch_dir,
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

    return PauseDispatchSnapshot(
        run_id=latest.id,
        dispatch_seq=seq_dirs[0].name,
        dispatch_markdown=format_pause_reason(latest),
        dispatch_dir=seq_dirs[0],
    )
