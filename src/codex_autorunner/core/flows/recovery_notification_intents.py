from __future__ import annotations

from pathlib import Path

from ..config import ConfigError, load_repo_config
from .models import FlowRunStatus
from .store import FlowNotificationIntentRecord, FlowStore
from .ux_helpers import build_flow_status_snapshot


def list_active_ticket_flow_notification_intents(
    workspace_root: Path,
) -> list[FlowNotificationIntentRecord]:
    """Return unresolved ledger rows for notification intents still present on a flow snapshot."""
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return []
    try:
        config = load_repo_config(workspace_root)
        durable_writes = config.durable_writes
    except ConfigError:
        durable_writes = False
    active_intent_ids: set[str] = set()
    with FlowStore(db_path, durable=durable_writes) as store:
        for record in store.list_flow_runs(flow_type="ticket_flow"):
            if record.status == FlowRunStatus.SUPERSEDED:
                continue
            snapshot = build_flow_status_snapshot(workspace_root, record, store)
            run_state = snapshot.get("run_state")
            if not isinstance(run_state, dict):
                continue
            for intent in run_state.get("notification_intents", []):
                if not isinstance(intent, dict):
                    continue
                intent_id = intent.get("intent_id")
                if isinstance(intent_id, str) and intent_id.strip():
                    active_intent_ids.add(intent_id)
        return [
            intent
            for intent in store.list_notification_intents(resolved=False)
            if intent.intent_id in active_intent_ids
        ]


def mark_ticket_flow_notification_intent_delivered(
    workspace_root: Path,
    intent_id: str,
    *,
    transport_key: str,
    record_id: str,
) -> None:
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return
    try:
        config = load_repo_config(workspace_root)
        durable_writes = config.durable_writes
    except ConfigError:
        durable_writes = False
    with FlowStore(db_path, durable=durable_writes) as store:
        store.mark_notification_intent_delivered(
            intent_id,
            transport=transport_key,
            attempt={"status": "enqueued", "record_id": record_id},
        )
