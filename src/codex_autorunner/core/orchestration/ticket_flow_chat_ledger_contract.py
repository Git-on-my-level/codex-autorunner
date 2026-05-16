from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

from ..text_utils import _normalize_optional_text

TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION = "ticket_flow_chat_ledger.v1"
TICKET_FLOW_FLOW_TYPE = "ticket_flow"
TICKET_FLOW_THREAD_KIND = "ticket_flow"

TicketFlowLedgerSource = Literal["flows.db", "orchestration"]
TicketFlowLedgerArtifact = Literal[
    "flow_run",
    "flow_event",
    "thread_target",
    "thread_execution",
    "thread_link",
    "chat_surface_event",
]
TicketFlowLedgerLifecycle = Literal[
    "flow_run.started",
    "flow_run.completed",
    "flow_run.failed",
    "ticket.selected",
    "managed_thread.created",
    "managed_thread.reused",
    "ticket_thread.linked",
    "ticket_turn.started",
    "ticket_turn.completed",
    "ticket_turn.failed",
]


@dataclass(frozen=True)
class TicketFlowLedgerRecord:
    """One canonical fact required for ticket-flow chat visibility."""

    name: str
    source: TicketFlowLedgerSource
    artifact: TicketFlowLedgerArtifact
    required_fields: tuple[str, ...]
    rebuilds: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TicketFlowThreadLink:
    """Repairable link between a ticket-flow turn and its managed thread."""

    flow_run_id: str
    ticket_id: str
    managed_thread_id: str
    workspace_root: str
    repo_id: Optional[str] = None
    worktree_id: Optional[str] = None
    turn_id: Optional[str] = None
    ticket_path: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def link_key(self) -> str:
        return ticket_flow_thread_link_key(self.flow_run_id, self.ticket_id)

    def to_thread_metadata(self) -> dict[str, Any]:
        return ticket_flow_thread_metadata(
            flow_run_id=self.flow_run_id,
            ticket_id=self.ticket_id,
            workspace_root=self.workspace_root,
            repo_id=self.repo_id,
            worktree_id=self.worktree_id,
            turn_id=self.turn_id,
            ticket_path=self.ticket_path,
            extra=self.metadata,
        )


TICKET_FLOW_LEDGER_LIFECYCLE: tuple[TicketFlowLedgerLifecycle, ...] = (
    "flow_run.started",
    "ticket.selected",
    "managed_thread.created",
    "managed_thread.reused",
    "ticket_thread.linked",
    "ticket_turn.started",
    "ticket_turn.completed",
    "ticket_turn.failed",
    "flow_run.completed",
    "flow_run.failed",
)

TICKET_FLOW_LEDGER_RECORDS: tuple[TicketFlowLedgerRecord, ...] = (
    TicketFlowLedgerRecord(
        name="repo-local flow run state",
        source="flows.db",
        artifact="flow_run",
        required_fields=(
            "run_id",
            "flow_type",
            "status",
            "current_step",
            "state.ticket_engine",
            "created_at",
            "updated_at",
        ),
        notes=(
            "The ticket-flow engine keeps sequencing, pause/resume, and terminal "
            "execution state here. This store is not sufficient for Web Hub chat "
            "visibility."
        ),
    ),
    TicketFlowLedgerRecord(
        name="orchestration flow lifecycle event",
        source="orchestration",
        artifact="flow_event",
        required_fields=(
            "event_type",
            "flow_run_id",
            "flow_type",
            "workspace_root",
            "occurred_at",
        ),
        rebuilds=("future_flow_index", "status_notifications"),
        notes="Required for flow start, completion, and failure projections.",
    ),
    TicketFlowLedgerRecord(
        name="orchestration ticket selection event",
        source="orchestration",
        artifact="flow_event",
        required_fields=(
            "event_type",
            "flow_run_id",
            "ticket_id",
            "ticket_path",
            "workspace_root",
            "occurred_at",
        ),
        rebuilds=("future_flow_index", "ticket_thread_grouping"),
        notes="Records the selected ticket before an agent turn is executed.",
    ),
    TicketFlowLedgerRecord(
        name="managed ticket-flow thread target",
        source="orchestration",
        artifact="thread_target",
        required_fields=(
            "managed_thread_id",
            "agent_id",
            "workspace_root",
            "thread_kind",
            "flow_type",
            "flow_run_id",
            "ticket_id",
        ),
        rebuilds=("web_hub_chat_index", "web_hub_chat_detail"),
        notes=(
            "Stored in orch_thread_targets. The metadata fields make ticket-flow "
            "threads visible without reading repo-local flow chat JSONL files."
        ),
    ),
    TicketFlowLedgerRecord(
        name="ticket-flow thread link",
        source="orchestration",
        artifact="thread_link",
        required_fields=(
            "flow_run_id",
            "ticket_id",
            "managed_thread_id",
            "workspace_root",
        ),
        rebuilds=("repair_backfill", "future_flow_index", "web_hub_chat_index"),
        notes=(
            "The durable invariant is exactly one repairable link for every "
            "executed ticket-flow agent turn."
        ),
    ),
    TicketFlowLedgerRecord(
        name="ticket-flow managed turn execution",
        source="orchestration",
        artifact="thread_execution",
        required_fields=(
            "turn_id",
            "managed_thread_id",
            "flow_run_id",
            "ticket_id",
            "status",
            "started_at",
            "finished_at",
        ),
        rebuilds=("web_hub_chat_index", "web_hub_chat_detail", "status_notifications"),
        notes="Records ticket-flow turn start, completion, and failure.",
    ),
    TicketFlowLedgerRecord(
        name="ticket-flow chat surface event",
        source="orchestration",
        artifact="chat_surface_event",
        required_fields=(
            "event_type",
            "surface_kind",
            "surface_key",
            "managed_thread_id",
            "workspace_root",
            "occurred_at",
        ),
        rebuilds=("web_hub_chat_index", "web_hub_chat_detail"),
        notes=(
            "Surface bindings and status changes are projections from the managed "
            "thread, not source-of-truth flow status messages."
        ),
    ),
)


def ticket_flow_thread_link_key(flow_run_id: Any, ticket_id: Any) -> str:
    normalized_run = _require_text(flow_run_id, "flow_run_id")
    normalized_ticket = _require_text(ticket_id, "ticket_id")
    return f"ticket_flow:{normalized_run}:{normalized_ticket}"


def ticket_flow_thread_metadata(
    *,
    flow_run_id: Any,
    ticket_id: Any,
    workspace_root: Any,
    repo_id: Any = None,
    worktree_id: Any = None,
    turn_id: Any = None,
    ticket_path: Any = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the metadata shape Web Hub read models use for ticket-flow chats."""

    run_id = _require_text(flow_run_id, "flow_run_id")
    normalized_ticket_id = _require_text(ticket_id, "ticket_id")
    normalized_workspace = _require_text(workspace_root, "workspace_root")
    payload: dict[str, Any] = dict(extra or {})
    payload.update(
        {
            "contract_version": TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION,
            "flow_type": TICKET_FLOW_FLOW_TYPE,
            "thread_kind": TICKET_FLOW_THREAD_KIND,
            "run_id": run_id,
            "flow_run_id": run_id,
            "ticket_id": normalized_ticket_id,
            "workspace_root": normalized_workspace,
            "ticket_flow_link_key": ticket_flow_thread_link_key(
                run_id, normalized_ticket_id
            ),
        }
    )
    for key, value in {
        "repo_id": repo_id,
        "worktree_id": worktree_id,
        "turn_id": turn_id,
        "ticket_path": ticket_path,
    }.items():
        normalized = _normalize_optional_text(value)
        if normalized is not None:
            payload[key] = normalized
    return payload


def ticket_flow_chat_ledger_contract() -> dict[str, Any]:
    """Return a serializable description of the canonical ledger contract."""

    return {
        "contract_version": TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION,
        "flow_type": TICKET_FLOW_FLOW_TYPE,
        "thread_kind": TICKET_FLOW_THREAD_KIND,
        "lifecycle_events": list(TICKET_FLOW_LEDGER_LIFECYCLE),
        "records": [record.to_dict() for record in TICKET_FLOW_LEDGER_RECORDS],
        "invariant": (
            "Every executed ticket-flow agent turn must have a repairable "
            "flow_run_id + ticket_id -> managed_thread_id link in orchestration "
            "state before the turn is allowed to complete."
        ),
        "read_models": {
            "web_hub_chat_index": (
                "Rebuild from orch_thread_targets, orch_thread_executions, "
                "orch_bindings, delivery ledgers, and orch_chat_surface_events. "
                "It must not read repo-local flows.db or flow chat JSONL files."
            ),
            "future_flow_index": (
                "Rebuild from orchestration flow lifecycle, ticket selection, "
                "thread-link, and turn lifecycle facts. Repo-local flows.db may "
                "be used only as engine state or as repair input."
            ),
        },
    }


def validate_ticket_flow_thread_metadata(metadata: Mapping[str, Any]) -> None:
    required = {
        "flow_type": TICKET_FLOW_FLOW_TYPE,
        "thread_kind": TICKET_FLOW_THREAD_KIND,
    }
    for key, expected in required.items():
        actual = _normalize_optional_text(metadata.get(key))
        if actual != expected:
            raise ValueError(f"ticket-flow thread metadata requires {key}={expected!r}")
    for key in ("run_id", "flow_run_id", "ticket_id", "workspace_root"):
        _require_text(metadata.get(key), key)
    if _normalize_optional_text(metadata.get("run_id")) != _normalize_optional_text(
        metadata.get("flow_run_id")
    ):
        raise ValueError("ticket-flow thread metadata run_id must match flow_run_id")


def _require_text(value: Any, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


__all__ = [
    "TICKET_FLOW_CHAT_LEDGER_CONTRACT_VERSION",
    "TICKET_FLOW_FLOW_TYPE",
    "TICKET_FLOW_LEDGER_LIFECYCLE",
    "TICKET_FLOW_LEDGER_RECORDS",
    "TICKET_FLOW_THREAD_KIND",
    "TicketFlowLedgerRecord",
    "TicketFlowThreadLink",
    "ticket_flow_chat_ledger_contract",
    "ticket_flow_thread_link_key",
    "ticket_flow_thread_metadata",
    "validate_ticket_flow_thread_metadata",
]
