from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ...tickets.frontmatter import parse_markdown_frontmatter
from ..flows.models import FlowEvent, FlowEventType, FlowRunRecord, FlowRunStatus
from ..flows.store import FlowStore
from ..managed_thread_store import ManagedThreadStore
from ..state_roots import resolve_repo_flows_db_path
from .ticket_flow_chat_ledger_contract import (
    ticket_flow_thread_link_key,
    ticket_flow_thread_metadata,
    validate_ticket_flow_thread_metadata,
)
from .turn_execution_contract import TurnExecutionOrigin, TurnExecutionRequest


@dataclass(frozen=True)
class TicketFlowVisibilityRepairDiagnostic:
    run_id: str
    status: str
    reason: str
    ticket_id: Optional[str] = None
    ticket_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "ticket_id": self.ticket_id,
            "ticket_path": self.ticket_path,
        }


@dataclass(frozen=True)
class TicketFlowVisibilityRepairAction:
    run_id: str
    ticket_id: str
    ticket_path: str
    action: str
    managed_thread_id: Optional[str] = None
    diagnostic: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ticket_id": self.ticket_id,
            "ticket_path": self.ticket_path,
            "action": self.action,
            "managed_thread_id": self.managed_thread_id,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class TicketFlowVisibilityRepairReport:
    repo_root: str
    hub_root: str
    scanned_runs: int = 0
    repaired: int = 0
    already_linked: int = 0
    dry_run: bool = False
    actions: tuple[TicketFlowVisibilityRepairAction, ...] = ()
    diagnostics: tuple[TicketFlowVisibilityRepairDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "hub_root": self.hub_root,
            "scanned_runs": self.scanned_runs,
            "repaired": self.repaired,
            "already_linked": self.already_linked,
            "dry_run": self.dry_run,
            "actions": [action.to_dict() for action in self.actions],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True)
class TicketFlowProjectionGap:
    repo_root: str
    hub_root: str
    run_id: str
    status: str
    reason: str
    ticket_id: Optional[str] = None
    ticket_path: Optional[str] = None
    expected_link_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "hub_root": self.hub_root,
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "ticket_id": self.ticket_id,
            "ticket_path": self.ticket_path,
            "expected_link_key": self.expected_link_key,
        }


@dataclass(frozen=True)
class _TicketTurnEvidence:
    run_id: str
    ticket_path: str
    ticket_id: str
    agent_id: str
    profile: Optional[str] = None
    legacy_thread_target_id: Optional[str] = None
    turn_id: Optional[str] = None
    source: str = "flow_events"
    metadata: dict[str, Any] = field(default_factory=dict)


def repair_ticket_flow_chat_visibility(
    *,
    repo_root: Path,
    hub_root: Path,
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    dry_run: bool = False,
    durable: bool = True,
) -> TicketFlowVisibilityRepairReport:
    """Backfill ticket-flow managed-thread links from conservative runtime evidence."""

    normalized_repo_root = repo_root.resolve()
    normalized_hub_root = hub_root.resolve()
    db_path = resolve_repo_flows_db_path(normalized_repo_root)
    actions: list[TicketFlowVisibilityRepairAction] = []
    diagnostics: list[TicketFlowVisibilityRepairDiagnostic] = []
    repaired = 0
    already_linked = 0

    if not db_path.exists():
        return TicketFlowVisibilityRepairReport(
            repo_root=str(normalized_repo_root),
            hub_root=str(normalized_hub_root),
            dry_run=dry_run,
            diagnostics=(
                TicketFlowVisibilityRepairDiagnostic(
                    run_id=run_id or "",
                    status="unrecoverable",
                    reason=f"missing flow store: {db_path}",
                ),
            ),
        )

    with FlowStore(db_path, durable=durable) as flow_store:
        if run_id:
            record = flow_store.get_flow_run(run_id)
            records = [record] if record is not None else []
            if not records:
                diagnostics.append(
                    TicketFlowVisibilityRepairDiagnostic(
                        run_id=run_id,
                        status="unrecoverable",
                        reason="flow run not found",
                    )
                )
        else:
            records = flow_store.list_flow_runs(flow_type="ticket_flow")
        thread_store = ManagedThreadStore(normalized_hub_root, durable=durable)
        for record in records:
            if record.flow_type != "ticket_flow":
                continue
            if not _is_repairable_terminal(record):
                diagnostics.append(
                    TicketFlowVisibilityRepairDiagnostic(
                        run_id=record.id,
                        status="skipped",
                        reason=f"run status is not terminal/recoverable: {record.status.value}",
                    )
                )
                continue
            evidence_items = list(
                _collect_ticket_turn_evidence(
                    record,
                    flow_store=flow_store,
                    repo_root=normalized_repo_root,
                )
            )
            if not evidence_items:
                diagnostics.append(
                    TicketFlowVisibilityRepairDiagnostic(
                        run_id=record.id,
                        status="unrecoverable",
                        reason=(
                            "missing repairable ticket-turn evidence in flow events "
                            "or ticket thread state"
                        ),
                    )
                )
                continue
            for evidence in evidence_items:
                existing = _find_existing_ticket_flow_thread(
                    thread_store,
                    evidence=evidence,
                    repo_root=normalized_repo_root,
                    repo_id=repo_id,
                )
                if existing is not None:
                    already_linked += 1
                    actions.append(
                        TicketFlowVisibilityRepairAction(
                            run_id=evidence.run_id,
                            ticket_id=evidence.ticket_id,
                            ticket_path=evidence.ticket_path,
                            action="already_linked",
                            managed_thread_id=existing,
                        )
                    )
                    continue
                if dry_run:
                    actions.append(
                        TicketFlowVisibilityRepairAction(
                            run_id=evidence.run_id,
                            ticket_id=evidence.ticket_id,
                            ticket_path=evidence.ticket_path,
                            action="would_repair",
                        )
                    )
                    continue
                created = _create_repaired_ticket_flow_thread(
                    thread_store,
                    evidence=evidence,
                    repo_root=normalized_repo_root,
                    repo_id=repo_id,
                )
                repaired += 1
                actions.append(
                    TicketFlowVisibilityRepairAction(
                        run_id=evidence.run_id,
                        ticket_id=evidence.ticket_id,
                        ticket_path=evidence.ticket_path,
                        action="repaired",
                        managed_thread_id=created,
                    )
                )

    return TicketFlowVisibilityRepairReport(
        repo_root=str(normalized_repo_root),
        hub_root=str(normalized_hub_root),
        scanned_runs=len(records),
        repaired=repaired,
        already_linked=already_linked,
        dry_run=dry_run,
        actions=tuple(actions),
        diagnostics=tuple(diagnostics),
    )


def diagnose_ticket_flow_projection_gaps(
    *,
    repo_root: Path,
    hub_root: Path,
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    durable: bool = True,
) -> tuple[TicketFlowProjectionGap, ...]:
    """Return terminal ticket-flow turns in flows.db that lack hub chat links."""

    normalized_repo_root = repo_root.resolve()
    normalized_hub_root = hub_root.resolve()
    db_path = resolve_repo_flows_db_path(normalized_repo_root)
    if not db_path.exists():
        return ()

    gaps: list[TicketFlowProjectionGap] = []
    with FlowStore(db_path, durable=durable) as flow_store:
        if run_id:
            record = flow_store.get_flow_run(run_id)
            records = [record] if record is not None else []
        else:
            records = flow_store.list_flow_runs(flow_type="ticket_flow")
        thread_store = ManagedThreadStore(normalized_hub_root, durable=durable)
        for record in records:
            if record is None or record.flow_type != "ticket_flow":
                continue
            if not _is_repairable_terminal(record):
                continue
            evidence_items = list(
                _collect_ticket_turn_evidence(
                    record,
                    flow_store=flow_store,
                    repo_root=normalized_repo_root,
                )
            )
            if not evidence_items:
                gaps.append(
                    TicketFlowProjectionGap(
                        repo_root=str(normalized_repo_root),
                        hub_root=str(normalized_hub_root),
                        run_id=record.id,
                        status=record.status.value,
                        reason=(
                            "repo-local flows.db has a terminal ticket-flow run, "
                            "but no repairable ticket-turn evidence that can be "
                            "projected into a Web Hub managed-thread row"
                        ),
                    )
                )
                continue
            for evidence in evidence_items:
                existing = _find_existing_ticket_flow_thread(
                    thread_store,
                    evidence=evidence,
                    repo_root=normalized_repo_root,
                    repo_id=repo_id,
                )
                if existing is not None:
                    continue
                gaps.append(
                    TicketFlowProjectionGap(
                        repo_root=str(normalized_repo_root),
                        hub_root=str(normalized_hub_root),
                        run_id=evidence.run_id,
                        status=record.status.value,
                        reason=(
                            "repo-local flows.db has an executed ticket-flow turn "
                            "without a canonical orchestration managed-thread link"
                        ),
                        ticket_id=evidence.ticket_id,
                        ticket_path=evidence.ticket_path,
                        expected_link_key=ticket_flow_thread_link_key(
                            evidence.run_id, evidence.ticket_id
                        ),
                    )
                )
    return tuple(gaps)


def _is_repairable_terminal(record: FlowRunRecord) -> bool:
    return record.status in {
        FlowRunStatus.COMPLETED,
        FlowRunStatus.FAILED,
        FlowRunStatus.STOPPED,
    }


def _collect_ticket_turn_evidence(
    record: FlowRunRecord,
    *,
    flow_store: FlowStore,
    repo_root: Path,
) -> Iterable[_TicketTurnEvidence]:
    seen: set[tuple[str, str]] = set()
    state = record.state if isinstance(record.state, dict) else {}
    engine = state.get("ticket_engine")
    engine = engine if isinstance(engine, dict) else {}

    for evidence in _evidence_from_ticket_thread_state(record, repo_root=repo_root):
        key = (evidence.run_id, evidence.ticket_id)
        seen.add(key)
        yield evidence

    selected_ticket: Optional[str] = None
    turn_id: Optional[str] = None
    for event in flow_store.get_events(record.id):
        if event.step_id != "ticket_turn":
            continue
        if event.event_type == FlowEventType.STEP_PROGRESS:
            ticket = _selected_ticket_from_event(event)
            if ticket is not None:
                selected_ticket = ticket
                turn_id = None
        elif event.event_type == FlowEventType.AGENT_STREAM_DELTA:
            turn_id = _text(event.data.get("turn_id")) or turn_id
        elif (
            event.event_type
            in {
                FlowEventType.STEP_COMPLETED,
                FlowEventType.STEP_FAILED,
                FlowEventType.AGENT_FAILED,
            }
            and selected_ticket
        ):
            path_evidence = _evidence_from_ticket_path(
                record,
                repo_root=repo_root,
                ticket_path=selected_ticket,
                agent_id=_text(engine.get("last_agent_id")) or "codex",
                profile=_text(engine.get("profile")),
                turn_id=turn_id,
                source="flow_events",
                require_done=event.event_type == FlowEventType.STEP_COMPLETED,
            )
            selected_ticket = None
            turn_id = None
            if path_evidence is None:
                continue
            key = (path_evidence.run_id, path_evidence.ticket_id)
            if key in seen:
                continue
            seen.add(key)
            yield path_evidence


def _evidence_from_ticket_thread_state(
    record: FlowRunRecord,
    *,
    repo_root: Path,
) -> Iterable[_TicketTurnEvidence]:
    state = record.state if isinstance(record.state, dict) else {}
    engine = state.get("ticket_engine")
    engine = engine if isinstance(engine, dict) else {}
    bindings = engine.get("ticket_thread_bindings")
    if isinstance(bindings, dict):
        for raw_ticket_id, raw_payload in bindings.items():
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            ticket_path = _text(payload.get("ticket_path"))
            ticket_id = _text(raw_ticket_id)
            if ticket_id is None or ticket_path is None:
                continue
            yield _TicketTurnEvidence(
                run_id=record.id,
                ticket_path=ticket_path,
                ticket_id=ticket_id,
                agent_id=_text(payload.get("agent_id")) or "codex",
                profile=_text(payload.get("profile")),
                legacy_thread_target_id=_text(payload.get("thread_target_id")),
                source="ticket_thread_bindings",
            )
    debug = engine.get("ticket_thread_debug")
    if isinstance(debug, dict) and _text(debug.get("thread_target_id")):
        ticket_path = _text(debug.get("ticket_path"))
        ticket_id = _text(debug.get("ticket_id"))
        if ticket_path is not None and ticket_id is not None:
            yield _TicketTurnEvidence(
                run_id=record.id,
                ticket_path=ticket_path,
                ticket_id=ticket_id,
                agent_id=_text(debug.get("agent_id")) or "codex",
                profile=_text(debug.get("profile")),
                legacy_thread_target_id=_text(debug.get("thread_target_id")),
                source="ticket_thread_debug",
            )

    _ = repo_root


def _selected_ticket_from_event(event: FlowEvent) -> Optional[str]:
    if _text(event.data.get("message")) != "Selected ticket":
        return None
    return _text(event.data.get("current_ticket"))


def _evidence_from_ticket_path(
    record: FlowRunRecord,
    *,
    repo_root: Path,
    ticket_path: str,
    agent_id: str,
    profile: Optional[str],
    turn_id: Optional[str],
    source: str,
    require_done: bool = True,
) -> Optional[_TicketTurnEvidence]:
    path = (repo_root / ticket_path).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
        frontmatter, _body = parse_markdown_frontmatter(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(frontmatter, dict):
        return None
    if require_done and frontmatter.get("done") is not True:
        return None
    if not require_done and frontmatter.get("done") is not True and turn_id is None:
        return None
    ticket_id = _text(frontmatter.get("ticket_id"))
    if ticket_id is None:
        return None
    return _TicketTurnEvidence(
        run_id=record.id,
        ticket_path=ticket_path,
        ticket_id=ticket_id,
        agent_id=_text(frontmatter.get("agent")) or agent_id,
        profile=profile,
        turn_id=turn_id,
        source=source,
    )


def _find_existing_ticket_flow_thread(
    thread_store: ManagedThreadStore,
    *,
    evidence: _TicketTurnEvidence,
    repo_root: Path,
    repo_id: Optional[str],
) -> Optional[str]:
    link_key = ticket_flow_thread_link_key(evidence.run_id, evidence.ticket_id)
    candidates = thread_store.list_threads(
        agent=evidence.agent_id,
        repo_id=repo_id,
        limit=1000,
    )
    if repo_id is not None:
        candidates.extend(
            thread_store.list_threads(agent=evidence.agent_id, limit=1000)
        )
    seen: set[str] = set()
    for row in candidates:
        thread_id = _text(row.get("managed_thread_id") or row.get("thread_target_id"))
        if thread_id is None or thread_id in seen:
            continue
        seen.add(thread_id)
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if _text(row.get("workspace_root") or metadata.get("workspace_root")) != str(
            repo_root
        ):
            continue
        if repo_id is not None and _text(row.get("repo_id")) not in {None, repo_id}:
            continue
        if _text(metadata.get("ticket_flow_link_key")) == link_key:
            return thread_id
        if (
            _text(metadata.get("flow_run_id")) == evidence.run_id
            and _text(metadata.get("ticket_id")) == evidence.ticket_id
        ):
            return thread_id
    return None


def _create_repaired_ticket_flow_thread(
    thread_store: ManagedThreadStore,
    *,
    evidence: _TicketTurnEvidence,
    repo_root: Path,
    repo_id: Optional[str],
) -> str:
    metadata = ticket_flow_thread_metadata(
        flow_run_id=evidence.run_id,
        ticket_id=evidence.ticket_id,
        workspace_root=str(repo_root),
        repo_id=repo_id,
        ticket_path=evidence.ticket_path,
        turn_id=evidence.turn_id,
        extra={
            "agent_profile": evidence.profile,
            "repair_provenance": {
                "source": evidence.source,
                "legacy_thread_target_id": evidence.legacy_thread_target_id,
                "backfilled": True,
            },
        },
    )
    metadata = _drop_none(metadata)
    validate_ticket_flow_thread_metadata(metadata)
    thread = thread_store.create_thread(
        evidence.agent_id,
        repo_root,
        repo_id=repo_id,
        name=f"Ticket Flow {evidence.ticket_path} ({evidence.agent_id})",
        metadata=metadata,
    )
    thread_id = str(thread["managed_thread_id"])
    prompt = (
        "Backfilled ticket-flow visibility placeholder. Original runtime prompt "
        "is unavailable in canonical orchestration records."
    )
    turn = thread_store.create_turn(
        thread_id,
        prompt=prompt,
        metadata={
            "repair_provenance": metadata["repair_provenance"],
            "flow_run_id": evidence.run_id,
            "ticket_id": evidence.ticket_id,
            "ticket_path": evidence.ticket_path,
        },
        turn_request=TurnExecutionRequest(
            request_id=uuid.uuid4().hex,
            target_id=thread_id,
            target_kind="thread",
            workspace_root=str(repo_root),
            request_kind="recovery",
            busy_policy="reject",
            prompt_text=prompt,
            agent=evidence.agent_id,
            approval_policy="never",
            sandbox_policy="dangerFullAccess",
            origin=TurnExecutionOrigin(
                kind="recovery",
                source_id=evidence.run_id,
                metadata={"source": "ticket_flow_visibility_repair"},
            ),
            metadata={
                "repair_provenance": metadata["repair_provenance"],
                "flow_run_id": evidence.run_id,
                "ticket_id": evidence.ticket_id,
                "ticket_path": evidence.ticket_path,
            },
        ),
    )
    thread_store.mark_turn_finished(
        str(turn["managed_turn_id"]),
        status="ok",
        assistant_text=(
            "Ticket-flow chat visibility was repaired from repo-local runtime "
            "evidence. The original transcript remains in legacy flow artifacts "
            "if available."
        ),
    )
    return thread_id


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            cleaned[key] = _drop_none(value)
        elif value is not None:
            cleaned[key] = value
    return cleaned


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "TicketFlowProjectionGap",
    "TicketFlowVisibilityRepairAction",
    "TicketFlowVisibilityRepairDiagnostic",
    "TicketFlowVisibilityRepairReport",
    "diagnose_ticket_flow_projection_gaps",
    "repair_ticket_flow_chat_visibility",
]
