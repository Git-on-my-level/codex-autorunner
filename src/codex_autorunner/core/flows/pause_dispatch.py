from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import load_repo_config
from ..redaction import redact_text
from .models import FlowRunRecord, FlowRunStatus
from .store import FlowStore


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


def load_latest_paused_ticket_flow_dispatch(
    workspace_root: Path,
) -> Optional[PauseDispatchSnapshot]:
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None

    config = load_repo_config(workspace_root)
    with FlowStore(db_path, durable=config.durable_writes) as store:
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
    seq = latest_dispatch_seq(history_dir)
    if not seq:
        return PauseDispatchSnapshot(
            run_id=latest.id,
            dispatch_seq="paused",
            dispatch_markdown=format_pause_reason(latest),
            dispatch_dir=None,
        )

    message_path = history_dir / seq / "DISPATCH.md"
    try:
        content = message_path.read_text(encoding="utf-8")
    except OSError:
        return None

    return PauseDispatchSnapshot(
        run_id=latest.id,
        dispatch_seq=seq,
        dispatch_markdown=content,
        dispatch_dir=history_dir / seq,
    )
