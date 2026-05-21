from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from ..automation import AutomationEvent, AutomationRuleEngine, AutomationStore
from ..car_context import CarContextProfile, normalize_car_context_profile
from ..domain.refs import ScopeRef
from ..logging_utils import log_event
from ..managed_thread_store import ManagedThreadStore
from .execution_result_coordinator import ExecutionResultCoordinator
from .interfaces import ThreadExecutionStore
from .managed_turn_lifecycle_contract import (
    ManagedTurnLifecyclePhase,
    ManagedTurnPhaseTransitionDecision,
    ManagedTurnTerminalStatus,
    plan_managed_turn_phase_transition,
)
from .models import ExecutionRecord, MessageRequestKind, ThreadTarget
from .runtime_bindings import RuntimeThreadBinding
from .thread_titles import choose_owned_thread_title

logger = logging.getLogger(__name__)


class _UnsetSentinel:
    pass


_UNSET = _UnsetSentinel()
_MANAGED_TURN_LIFECYCLE_PHASE_KEY = "managed_turn_lifecycle_phase"


def _resolve_optional_patch_value(
    value: Optional[str] | _UnsetSentinel,
    current: Optional[str],
) -> Optional[str]:
    if isinstance(value, _UnsetSentinel):
        return current
    return value


def _resolve_binding_state_patch_value(
    value: Optional[str] | _UnsetSentinel,
    current: Optional[str],
) -> str:
    if isinstance(value, _UnsetSentinel):
        return current or "bound"
    return value or "bound"


def _notify_pma_lifecycle_automation_transition(
    hub_root: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    store = AutomationStore(hub_root)
    event_id_source = (
        _optional_text(payload.get("transition_id"))
        or _optional_text(payload.get("idempotency_key"))
        or _optional_text(payload.get("managed_turn_id"))
        or _optional_text(payload.get("thread_id"))
        or "managed-thread-transition"
    )
    automation_event = AutomationEvent.create(
        event_id=f"lifecycle:{event_id_source}",
        event_type=_automation_event_type_from_transition(payload),
        source="lifecycle",
        repo_id=_optional_text(payload.get("repo_id")) or "",
        target={
            "repo_id": _optional_text(payload.get("repo_id")),
            "run_id": _optional_text(payload.get("run_id")),
            "thread_id": _optional_text(payload.get("thread_id")),
        },
        payload={
            **dict(payload),
            "origin": _optional_text(payload.get("origin")) or "orchestration",
        },
        raw_payload=dict(payload),
        metadata={"lifecycle_event_id": event_id_source},
        observed_at=_optional_text(payload.get("timestamp")),
    )
    result = AutomationRuleEngine(store).record_event_and_enqueue_jobs(automation_event)
    return {
        "status": "ok",
        "matched": result.matched_rules,
        "created": result.jobs_created,
        "deduped": result.jobs_deduped,
        "skipped": result.jobs_skipped,
    }


def _automation_event_type_from_transition(payload: Mapping[str, Any]) -> str:
    to_state = str(payload.get("to_state") or "").strip().lower()
    if to_state == "completed":
        return "lifecycle.flow_completed"
    if to_state in {"interrupted", "stopped"}:
        return "lifecycle.flow_stopped"
    if to_state in {"paused", "blocked"}:
        return "lifecycle.flow_paused"
    if to_state in {"running", "resumed"}:
        return "lifecycle.flow_resumed"
    return "lifecycle.flow_failed"


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _thread_target_from_store_row(record: Mapping[str, Any]) -> ThreadTarget:
    return ThreadTarget.from_mapping(record)


def _thread_target_from_store_row_with_runtime_binding(
    store: ManagedThreadStore, record: Mapping[str, Any]
) -> ThreadTarget:
    thread_record = dict(record)
    managed_thread_id = str(thread_record.get("managed_thread_id") or "").strip()
    runtime_binding = (
        store.get_thread_runtime_binding(managed_thread_id)
        if managed_thread_id
        else None
    )
    if runtime_binding is not None:
        thread_record["backend_thread_id"] = runtime_binding.backend_thread_id
        thread_record["backend_runtime_instance_id"] = (
            runtime_binding.backend_runtime_instance_id
        )
        thread_record["backend_binding_state"] = runtime_binding.binding_state
        thread_record["backend_binding_state_reason"] = runtime_binding.state_reason
    return ThreadTarget.from_mapping(thread_record)


def _execution_record_from_store_row(record: Mapping[str, Any]) -> ExecutionRecord:
    return ExecutionRecord.from_mapping(record)


class ManagedThreadExecutionStore(ThreadExecutionStore):
    """Adapter that hides PMA thread-store details behind orchestration nouns."""

    def __init__(self, store: ManagedThreadStore) -> None:
        self._store = store
        self._execution_results = ExecutionResultCoordinator(
            get_execution=self.get_execution,
            get_thread_target=self.get_thread_target,
            mark_turn_finished=self._store.mark_turn_finished,
            mark_turn_interrupted=self._store.mark_turn_interrupted,
            advance_lifecycle_phase=self._advance_execution_lifecycle_phase,
            notify_transition=lambda payload: _notify_pma_lifecycle_automation_transition(
                self._store.hub_root, payload
            ),
            logger=logger,
        )

    @property
    def hub_root(self) -> Path:
        return self._store.hub_root

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        scope: Optional[ScopeRef] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        metadata_payload = dict(metadata or {})
        normalized_context_profile = normalize_car_context_profile(context_profile)
        if normalized_context_profile is not None:
            metadata_payload["context_profile"] = normalized_context_profile
        created = self._store.create_thread(
            agent_id,
            workspace_root,
            scope=scope,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            name=display_name,
            backend_thread_id=backend_thread_id,
            metadata=metadata_payload,
        )
        return _thread_target_from_store_row(created)

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        return _thread_target_from_store_row_with_runtime_binding(self._store, record)

    def get_thread_runtime_binding(
        self, thread_target_id: str
    ) -> Optional[RuntimeThreadBinding]:
        return self._store.get_thread_runtime_binding(thread_target_id)

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        return [
            _thread_target_from_store_row_with_runtime_binding(self._store, record)
            for record in self._store.list_threads(
                agent=agent_id,
                status=lifecycle_status,
                normalized_status=runtime_status,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                limit=limit,
            )
        ]

    def list_thread_ids_with_running_executions(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        return self._store.list_thread_ids_with_running_executions(limit=limit)

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] | _UnsetSentinel = _UNSET,
        backend_runtime_instance_id: Optional[str] | _UnsetSentinel = _UNSET,
        binding_state: Optional[str] | _UnsetSentinel = _UNSET,
        state_reason: Optional[str] | _UnsetSentinel = _UNSET,
    ) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        if (
            backend_thread_id is not _UNSET
            or backend_runtime_instance_id is not _UNSET
            or binding_state is not _UNSET
            or state_reason is not _UNSET
        ):
            current_binding = self._store.get_thread_runtime_binding(thread_target_id)
            current_backend_thread_id = (
                current_binding.backend_thread_id
                if current_binding is not None
                else None
            )
            current_runtime_instance_id = (
                current_binding.backend_runtime_instance_id
                if current_binding is not None
                else None
            )
            current_binding_state = (
                current_binding.binding_state if current_binding is not None else None
            )
            current_state_reason = (
                current_binding.state_reason if current_binding is not None else None
            )
            self._store.set_thread_backend_binding(
                thread_target_id,
                _resolve_optional_patch_value(
                    backend_thread_id,
                    current_backend_thread_id,
                ),
                backend_runtime_instance_id=_resolve_optional_patch_value(
                    backend_runtime_instance_id,
                    current_runtime_instance_id,
                ),
                binding_state=_resolve_binding_state_patch_value(
                    binding_state,
                    current_binding_state,
                ),
                state_reason=_resolve_optional_patch_value(
                    state_reason,
                    current_state_reason,
                ),
            )
        self._store.activate_thread(thread_target_id)
        updated = self._store.get_thread(thread_target_id)
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)

    def archive_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        record = self._store.get_thread(thread_target_id)
        if record is None:
            return None
        self._store.archive_thread(thread_target_id)
        updated = self._store.get_thread(thread_target_id)
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)

    def set_thread_backend_binding(
        self,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
        binding_state: str = "bound",
        state_reason: Optional[str] = None,
    ) -> None:
        self._store.set_thread_backend_binding(
            thread_target_id,
            backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
            binding_state=binding_state,
            state_reason=state_reason,
        )

    def mark_thread_runtime_binding_state(
        self,
        thread_target_id: str,
        *,
        binding_state: str,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return self._store.mark_thread_runtime_binding_state(
            thread_target_id,
            binding_state=binding_state,
            state_reason=state_reason,
        )

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        request_kind: MessageRequestKind = "message",
        busy_policy: str = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> ExecutionRecord:
        metadata_payload = dict(metadata or {})
        initialized_lifecycle_phase = (
            _MANAGED_TURN_LIFECYCLE_PHASE_KEY not in metadata_payload
        )
        if initialized_lifecycle_phase:
            metadata_payload[_MANAGED_TURN_LIFECYCLE_PHASE_KEY] = "accepted"
        create_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "request_kind": request_kind,
            "busy_policy": busy_policy,
            "model": model,
            "reasoning": reasoning,
            "client_turn_id": client_request_id,
            "metadata": metadata_payload,
            "queue_payload": queue_payload,
        }
        try:
            created = self._store.create_turn(thread_target_id, **create_kwargs)
        except TypeError as exc:
            if "metadata" not in str(exc):
                raise
            create_kwargs.pop("metadata", None)
            created = self._store.create_turn(thread_target_id, **create_kwargs)
        record = _execution_record_from_store_row(created)
        if initialized_lifecycle_phase:
            self._advance_execution_lifecycle_phase(
                record.target_id,
                record.execution_id,
                to_phase="queued" if record.status == "queued" else "runtime_starting",
            )
        return self.get_execution(record.target_id, record.execution_id) or record

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        record = self._store.get_turn(thread_target_id, execution_id)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def get_previous_completed_execution(
        self,
        thread_target_id: str,
        *,
        exclude_execution_id: Optional[str] = None,
    ) -> Optional[ExecutionRecord]:
        getter = getattr(self._store, "get_previous_completed_turn", None)
        if not callable(getter):
            return None
        record = getter(
            thread_target_id,
            exclude_turn_id=exclude_execution_id,
        )
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        record = self._store.get_running_turn(thread_target_id)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def get_running_turn(self, thread_target_id: str) -> Optional[dict[str, Any]]:
        return self._store.get_running_turn(thread_target_id)

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        record = self._store.get_running_turn(thread_target_id)
        if record is None:
            record = next(iter(self._store.list_turns(thread_target_id, limit=1)), None)
        if record is None:
            return None
        return _execution_record_from_store_row(record)

    def list_turns(
        self, thread_target_id: str, *, limit: int = 50
    ) -> list[ExecutionRecord]:
        return [
            _execution_record_from_store_row(record)
            for record in self._store.list_turns(thread_target_id, limit=limit)
        ]

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        return [
            _execution_record_from_store_row(record)
            for record in self._store.list_queued_turns(thread_target_id, limit=limit)
        ]

    def get_queue_depth(self, thread_target_id: str) -> int:
        return self._store.get_queue_depth(thread_target_id)

    def cancel_queued_execution(self, thread_target_id: str, execution_id: str) -> bool:
        return self._store.cancel_queued_turn(thread_target_id, execution_id)

    def promote_queued_execution(
        self, thread_target_id: str, execution_id: str
    ) -> bool:
        return self._store.promote_queued_turn(thread_target_id, execution_id)

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[tuple[ExecutionRecord, dict[str, Any]]]:
        claimed = self._store.claim_next_queued_turn(thread_target_id)
        if claimed is None:
            return None
        execution, payload = claimed
        record = _execution_record_from_store_row(execution)
        self._advance_execution_lifecycle_phase(
            record.target_id,
            record.execution_id,
            to_phase="runtime_starting",
        )
        refreshed = self.get_execution(record.target_id, record.execution_id) or record
        return refreshed, payload

    def set_execution_backend_id(
        self,
        execution_id: str,
        backend_turn_id: Optional[str],
        *,
        confirmed_start: bool = True,
    ) -> None:
        self._store.set_turn_backend_turn_id(
            execution_id,
            backend_turn_id,
            confirmed_start=confirmed_start,
        )
        record = self._get_execution_by_id(execution_id)
        if record is None:
            return
        current_phase = str(
            record.metadata.get(_MANAGED_TURN_LIFECYCLE_PHASE_KEY) or ""
        ).strip()
        if not confirmed_start and current_phase == "runtime_running":
            return
        if current_phase in {
            "runtime_terminal_observed",
            "terminal_recording",
            "terminal_recorded",
            "delivery_enqueued",
            "side_effects_pending",
            "side_effects_complete",
        }:
            return
        self._advance_execution_lifecycle_phase(
            record.target_id,
            record.execution_id,
            to_phase="runtime_running" if confirmed_start else "runtime_starting",
        )

    def _get_execution_by_id(self, execution_id: str) -> Optional[ExecutionRecord]:
        getter = getattr(self._store, "get_turn_by_id", None)
        if callable(getter):
            record = getter(execution_id)
            if record is not None:
                return _execution_record_from_store_row(record)
        return None

    def _advance_execution_lifecycle_phase(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        to_phase: ManagedTurnLifecyclePhase,
        terminal_status: Optional[ManagedTurnTerminalStatus] = None,
    ) -> ManagedTurnPhaseTransitionDecision:
        execution = self.get_execution(thread_target_id, execution_id)
        if execution is None:
            raise KeyError(f"Execution '{execution_id}' is missing")
        decision = plan_managed_turn_phase_transition(
            managed_thread_id=thread_target_id,
            execution_id=execution_id,
            current_phase=execution.metadata.get(_MANAGED_TURN_LIFECYCLE_PHASE_KEY),
            next_phase=to_phase,
            terminal_status=terminal_status,
        )
        log_event(
            logger,
            logging.INFO if decision.action != "reject" else logging.WARNING,
            "orchestration.thread.managed_turn_lifecycle_phase",
            managed_thread_id=decision.managed_thread_id,
            execution_id=decision.execution_id,
            from_phase=decision.from_phase,
            to_phase=decision.to_phase,
            action=decision.action,
            reason=decision.reason,
            terminal_status=decision.terminal_status,
        )
        if decision.action == "reject":
            raise RuntimeError(
                "Illegal managed-turn lifecycle phase transition "
                f"for execution '{execution_id}': "
                f"{decision.from_phase!r} -> {decision.to_phase!r}"
            )
        if decision.should_persist:
            updater = getattr(self._store, "update_turn_metadata", None)
            if not callable(updater):
                raise RuntimeError("PMA store cannot persist execution metadata")
            updater(
                execution_id,
                {_MANAGED_TURN_LIFECYCLE_PHASE_KEY: decision.to_phase},
            )
        return decision

    def _notify_terminal_transition(
        self,
        *,
        thread_target_id: str,
        execution_id: str,
        status: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        self._execution_results.notify_terminal_transition(
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            status=status,
            error=error,
        )

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        return self._execution_results.record_execution_result(
            thread_target_id,
            execution_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            backend_turn_id=backend_turn_id,
            transcript_turn_id=transcript_turn_id,
        )

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        return self._execution_results.record_execution_interrupted(
            thread_target_id, execution_id
        )

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        return len(self._store.cancel_queued_turns(thread_target_id))

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None:
        self._store.update_thread_after_turn(
            thread_target_id,
            last_turn_id=execution_id,
            last_message_preview=message_preview,
        )
        self.update_thread_title(
            thread_target_id,
            choose_owned_thread_title(None, message_preview=message_preview),
            metadata={"car_title_source": "message_preview"},
        )

    def update_thread_title(
        self,
        thread_target_id: str,
        title: Optional[str],
        *,
        metadata: Optional[dict[str, Any]] = None,
        only_if_generic: bool = True,
    ) -> Optional[ThreadTarget]:
        updater = getattr(self._store, "update_thread_title", None)
        if not callable(updater):
            if metadata:
                self._store.update_thread_metadata(thread_target_id, metadata)
            return self.get_thread_target(thread_target_id)
        updated = updater(
            thread_target_id,
            title,
            metadata=metadata,
            only_if_generic=only_if_generic,
        )
        if updated is None:
            return None
        return _thread_target_from_store_row(updated)
