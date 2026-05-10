from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from ...core.orchestration.chat_operation_state import (
    ChatOperationSnapshot,
    ChatOperationState,
)


class SharedInterruptState(str, Enum):
    REQUESTED = "requested"
    BACKEND_DISPATCHED = "backend_dispatched"
    CONFIRMED = "confirmed"
    STILL_STOPPING = "still_stopping"
    ALREADY_FINISHED = "already_finished"
    FAILED_TO_DISPATCH = "failed_to_dispatch"


@dataclass(frozen=True)
class SharedInterruptOutcome:
    state: SharedInterruptState
    thread_target_id: str
    execution_id: Optional[str] = None
    referenced_execution_id: Optional[str] = None
    cancelled_queued: int = 0
    interrupted_active: bool = False
    recovered_lost_backend: bool = False
    duplicate_of_operation_id: Optional[str] = None
    backend_dispatch_attempted: bool = False
    error: Optional[str] = None
    operation: Optional[ChatOperationSnapshot] = None

    @property
    def terminal(self) -> bool:
        return self.state in {
            SharedInterruptState.CONFIRMED,
            SharedInterruptState.ALREADY_FINISHED,
            SharedInterruptState.FAILED_TO_DISPATCH,
        }


class _ManagedThreadInterruptService(Protocol):
    async def stop_thread(
        self,
        thread_target_id: str,
        *,
        cancel_queued: bool = True,
    ) -> Any: ...


class _InterruptOperationStore(Protocol):
    def patch_operation(
        self,
        operation_id: str,
        *,
        state: ChatOperationState | object,
        validate_transition: bool = True,
        metadata_updates: Optional[dict[str, Any]] = None,
        **changes: Any,
    ) -> Optional[ChatOperationSnapshot]: ...

    def list_operations_for_thread(
        self,
        thread_target_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 20,
    ) -> list[ChatOperationSnapshot]: ...


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _execution_id(execution: Any) -> Optional[str]:
    return _normalized_optional_text(getattr(execution, "execution_id", None))


def _interrupt_metadata(
    *,
    interrupt_state: SharedInterruptState,
    cancel_queued: bool,
    referenced_execution_id: Optional[str],
    duplicate_of_operation_id: Optional[str] = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "control": "interrupt",
        "interrupt_state": interrupt_state.value,
        "cancel_queued": bool(cancel_queued),
        "referenced_execution_id": referenced_execution_id,
    }
    if duplicate_of_operation_id:
        metadata["duplicate_of_operation_id"] = duplicate_of_operation_id
    return metadata


def _find_inflight_interrupt(
    store: _InterruptOperationStore,
    *,
    thread_target_id: str,
    execution_id: Optional[str],
    current_operation_id: Optional[str],
) -> Optional[ChatOperationSnapshot]:
    for operation in store.list_operations_for_thread(
        thread_target_id,
        include_terminal=False,
        limit=20,
    ):
        if current_operation_id and operation.operation_id == current_operation_id:
            continue
        metadata = dict(operation.metadata or {})
        if metadata.get("control") != "interrupt":
            continue
        interrupt_state = str(metadata.get("interrupt_state") or "").strip().lower()
        if interrupt_state not in {
            SharedInterruptState.REQUESTED.value,
            SharedInterruptState.BACKEND_DISPATCHED.value,
            SharedInterruptState.STILL_STOPPING.value,
        }:
            continue
        existing_execution_id = (
            _normalized_optional_text(metadata.get("referenced_execution_id"))
            or operation.execution_id
        )
        if (
            execution_id
            and existing_execution_id
            and existing_execution_id != execution_id
        ):
            continue
        return operation
    return None


def _write_interrupt_operation(
    store: Optional[_InterruptOperationStore],
    *,
    operation_id: Optional[str],
    thread_target_id: str,
    execution_id: Optional[str],
    cancel_queued: bool,
    referenced_execution_id: Optional[str],
    interrupt_state: SharedInterruptState,
    duplicate_of_operation_id: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[ChatOperationSnapshot]:
    if store is None:
        return None
    normalized_operation_id = _normalized_optional_text(operation_id)
    if normalized_operation_id is None:
        return None
    next_state = ChatOperationState.INTERRUPTING
    terminal_outcome: Optional[str] = None
    terminal_detail: Optional[str] = None
    if interrupt_state == SharedInterruptState.CONFIRMED:
        next_state = ChatOperationState.COMPLETED
        terminal_outcome = interrupt_state.value
    elif interrupt_state == SharedInterruptState.ALREADY_FINISHED:
        next_state = ChatOperationState.COMPLETED
        terminal_outcome = interrupt_state.value
    elif interrupt_state == SharedInterruptState.FAILED_TO_DISPATCH:
        next_state = ChatOperationState.FAILED
        terminal_outcome = interrupt_state.value
        terminal_detail = error
    return store.patch_operation(
        normalized_operation_id,
        state=next_state,
        validate_transition=False,
        thread_target_id=thread_target_id,
        execution_id=execution_id,
        terminal_outcome=terminal_outcome,
        terminal_detail=terminal_detail,
        metadata_updates=_interrupt_metadata(
            interrupt_state=interrupt_state,
            cancel_queued=cancel_queued,
            referenced_execution_id=referenced_execution_id,
            duplicate_of_operation_id=duplicate_of_operation_id,
        ),
    )


def _resolve_execution(
    service: _ManagedThreadInterruptService,
    *,
    thread_target_id: str,
    execution_id: Optional[str],
) -> Any:
    get_execution = getattr(service, "get_execution", None)
    if execution_id:
        if callable(get_execution):
            resolved = get_execution(thread_target_id, execution_id)
            if resolved is not None:
                return resolved
    get_latest_execution = getattr(service, "get_latest_execution", None)
    if callable(get_latest_execution):
        return get_latest_execution(thread_target_id)
    return None


async def request_managed_thread_interrupt(
    *,
    orchestration_service: _ManagedThreadInterruptService,
    thread_target_id: str,
    cancel_queued: bool = True,
    referenced_execution_id: Optional[str] = None,
    operation_store: Optional[_InterruptOperationStore] = None,
    operation_id: Optional[str] = None,
) -> SharedInterruptOutcome:
    normalized_thread_target_id = _normalized_optional_text(thread_target_id)
    if normalized_thread_target_id is None:
        raise ValueError("thread_target_id is required")
    normalized_execution_id = _normalized_optional_text(referenced_execution_id)
    get_running_execution = getattr(
        orchestration_service, "get_running_execution", None
    )
    running_execution = (
        get_running_execution(normalized_thread_target_id)
        if callable(get_running_execution)
        else None
    )
    running_execution_id = _execution_id(running_execution)

    if (
        normalized_execution_id is not None
        and running_execution_id is not None
        and running_execution_id != normalized_execution_id
    ):
        operation = _write_interrupt_operation(
            operation_store,
            operation_id=operation_id,
            thread_target_id=normalized_thread_target_id,
            execution_id=running_execution_id,
            cancel_queued=cancel_queued,
            referenced_execution_id=normalized_execution_id,
            interrupt_state=SharedInterruptState.ALREADY_FINISHED,
        )
        return SharedInterruptOutcome(
            state=SharedInterruptState.ALREADY_FINISHED,
            thread_target_id=normalized_thread_target_id,
            execution_id=running_execution_id,
            referenced_execution_id=normalized_execution_id,
            operation=operation,
        )

    inflight_interrupt = None
    if operation_store is not None:
        inflight_interrupt = _find_inflight_interrupt(
            operation_store,
            thread_target_id=normalized_thread_target_id,
            execution_id=normalized_execution_id or running_execution_id,
            current_operation_id=_normalized_optional_text(operation_id),
        )
    if inflight_interrupt is not None:
        inflight_execution_id = (
            _normalized_optional_text(
                dict(inflight_interrupt.metadata or {}).get("referenced_execution_id")
            )
            or inflight_interrupt.execution_id
        )
        if running_execution is None and inflight_execution_id is not None:
            resolved_execution = _resolve_execution(
                orchestration_service,
                thread_target_id=normalized_thread_target_id,
                execution_id=inflight_execution_id,
            )
            resolved_status = str(
                getattr(resolved_execution, "status", "") or ""
            ).strip()
            if resolved_execution is not None and resolved_status.lower() != "running":
                operation = None
                if operation_store is not None:
                    operation = operation_store.patch_operation(
                        inflight_interrupt.operation_id,
                        state=ChatOperationState.COMPLETED,
                        validate_transition=False,
                        terminal_outcome=SharedInterruptState.ALREADY_FINISHED.value,
                        metadata_updates=_interrupt_metadata(
                            interrupt_state=SharedInterruptState.ALREADY_FINISHED,
                            cancel_queued=cancel_queued,
                            referenced_execution_id=inflight_execution_id,
                        ),
                    )
                return SharedInterruptOutcome(
                    state=SharedInterruptState.ALREADY_FINISHED,
                    thread_target_id=normalized_thread_target_id,
                    execution_id=inflight_execution_id,
                    referenced_execution_id=normalized_execution_id
                    or inflight_execution_id,
                    duplicate_of_operation_id=inflight_interrupt.operation_id,
                    operation=operation,
                )
            else:
                operation = _write_interrupt_operation(
                    operation_store,
                    operation_id=operation_id,
                    thread_target_id=normalized_thread_target_id,
                    execution_id=inflight_execution_id,
                    cancel_queued=cancel_queued,
                    referenced_execution_id=normalized_execution_id
                    or inflight_execution_id,
                    interrupt_state=SharedInterruptState.STILL_STOPPING,
                    duplicate_of_operation_id=inflight_interrupt.operation_id,
                )
                return SharedInterruptOutcome(
                    state=SharedInterruptState.STILL_STOPPING,
                    thread_target_id=normalized_thread_target_id,
                    execution_id=inflight_execution_id,
                    referenced_execution_id=normalized_execution_id
                    or inflight_execution_id,
                    duplicate_of_operation_id=inflight_interrupt.operation_id,
                    operation=operation,
                )
        else:
            operation = _write_interrupt_operation(
                operation_store,
                operation_id=operation_id,
                thread_target_id=normalized_thread_target_id,
                execution_id=inflight_execution_id or running_execution_id,
                cancel_queued=cancel_queued,
                referenced_execution_id=normalized_execution_id
                or inflight_execution_id
                or running_execution_id,
                interrupt_state=SharedInterruptState.STILL_STOPPING,
                duplicate_of_operation_id=inflight_interrupt.operation_id,
            )
            return SharedInterruptOutcome(
                state=SharedInterruptState.STILL_STOPPING,
                thread_target_id=normalized_thread_target_id,
                execution_id=inflight_execution_id or running_execution_id,
                referenced_execution_id=normalized_execution_id
                or inflight_execution_id
                or running_execution_id,
                duplicate_of_operation_id=inflight_interrupt.operation_id,
                operation=operation,
            )
    _write_interrupt_operation(
        operation_store,
        operation_id=operation_id,
        thread_target_id=normalized_thread_target_id,
        execution_id=running_execution_id,
        cancel_queued=cancel_queued,
        referenced_execution_id=normalized_execution_id or running_execution_id,
        interrupt_state=SharedInterruptState.REQUESTED,
    )
    try:
        if cancel_queued:
            stop_outcome = await orchestration_service.stop_thread(
                normalized_thread_target_id
            )
        else:
            stop_outcome = await orchestration_service.stop_thread(
                normalized_thread_target_id,
                cancel_queued=False,
            )
    except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
        post_running = (
            get_running_execution(normalized_thread_target_id)
            if callable(get_running_execution)
            else None
        )
        post_running_execution_id = _execution_id(post_running)
        if post_running is None or (
            normalized_execution_id is not None
            and post_running_execution_id != normalized_execution_id
        ):
            operation = _write_interrupt_operation(
                operation_store,
                operation_id=operation_id,
                thread_target_id=normalized_thread_target_id,
                execution_id=post_running_execution_id,
                cancel_queued=cancel_queued,
                referenced_execution_id=normalized_execution_id or running_execution_id,
                interrupt_state=SharedInterruptState.ALREADY_FINISHED,
            )
            return SharedInterruptOutcome(
                state=SharedInterruptState.ALREADY_FINISHED,
                thread_target_id=normalized_thread_target_id,
                execution_id=post_running_execution_id,
                referenced_execution_id=normalized_execution_id or running_execution_id,
                operation=operation,
            )
        operation = _write_interrupt_operation(
            operation_store,
            operation_id=operation_id,
            thread_target_id=normalized_thread_target_id,
            execution_id=post_running_execution_id,
            cancel_queued=cancel_queued,
            referenced_execution_id=normalized_execution_id or running_execution_id,
            interrupt_state=SharedInterruptState.FAILED_TO_DISPATCH,
            error=str(exc).strip() or None,
        )
        return SharedInterruptOutcome(
            state=SharedInterruptState.FAILED_TO_DISPATCH,
            thread_target_id=normalized_thread_target_id,
            execution_id=post_running_execution_id,
            referenced_execution_id=normalized_execution_id or running_execution_id,
            backend_dispatch_attempted=True,
            error=str(exc).strip() or None,
            operation=operation,
        )

    interrupted_active = bool(getattr(stop_outcome, "interrupted_active", False))
    recovered_lost_backend = bool(
        getattr(stop_outcome, "recovered_lost_backend", False)
    )
    cancelled_queued = int(getattr(stop_outcome, "cancelled_queued", 0) or 0)
    execution = getattr(stop_outcome, "execution", None)
    resolved_execution_id = _execution_id(execution) or running_execution_id
    _write_interrupt_operation(
        operation_store,
        operation_id=operation_id,
        thread_target_id=normalized_thread_target_id,
        execution_id=resolved_execution_id,
        cancel_queued=cancel_queued,
        referenced_execution_id=normalized_execution_id or running_execution_id,
        interrupt_state=SharedInterruptState.BACKEND_DISPATCHED,
    )
    if interrupted_active or recovered_lost_backend or cancelled_queued:
        operation = _write_interrupt_operation(
            operation_store,
            operation_id=operation_id,
            thread_target_id=normalized_thread_target_id,
            execution_id=resolved_execution_id,
            cancel_queued=cancel_queued,
            referenced_execution_id=normalized_execution_id or running_execution_id,
            interrupt_state=SharedInterruptState.CONFIRMED,
        )
        return SharedInterruptOutcome(
            state=SharedInterruptState.CONFIRMED,
            thread_target_id=normalized_thread_target_id,
            execution_id=resolved_execution_id,
            referenced_execution_id=normalized_execution_id or running_execution_id,
            cancelled_queued=cancelled_queued,
            interrupted_active=interrupted_active,
            recovered_lost_backend=recovered_lost_backend,
            backend_dispatch_attempted=bool(
                interrupted_active or recovered_lost_backend or running_execution_id
            ),
            operation=operation,
        )
    operation = _write_interrupt_operation(
        operation_store,
        operation_id=operation_id,
        thread_target_id=normalized_thread_target_id,
        execution_id=resolved_execution_id,
        cancel_queued=cancel_queued,
        referenced_execution_id=normalized_execution_id or running_execution_id,
        interrupt_state=SharedInterruptState.ALREADY_FINISHED,
    )
    return SharedInterruptOutcome(
        state=SharedInterruptState.ALREADY_FINISHED,
        thread_target_id=normalized_thread_target_id,
        execution_id=resolved_execution_id,
        referenced_execution_id=normalized_execution_id or running_execution_id,
        operation=operation,
    )


def render_managed_thread_interrupt_message(
    outcome: SharedInterruptOutcome,
    *,
    active_turn_text: str = "Stopping current turn...",
    still_stopping_text: str = "Still stopping current turn...",
    already_finished_text: str = "Current turn already finished.",
    recovered_lost_backend_text: str = (
        "Recovered stale session after backend thread was lost."
    ),
    queued_text_template: str = "Cancelled {count} queued turn(s).",
) -> str:
    if outcome.state == SharedInterruptState.STILL_STOPPING:
        return still_stopping_text
    if outcome.state == SharedInterruptState.ALREADY_FINISHED:
        if outcome.cancelled_queued > 0:
            return queued_text_template.format(count=outcome.cancelled_queued)
        return already_finished_text
    if outcome.state == SharedInterruptState.FAILED_TO_DISPATCH:
        return "Interrupt failed. Please try again."
    if outcome.state == SharedInterruptState.CONFIRMED:
        parts: list[str] = []
        if outcome.recovered_lost_backend:
            parts.append(recovered_lost_backend_text)
        elif outcome.interrupted_active:
            parts.append(active_turn_text)
        if outcome.cancelled_queued > 0:
            parts.append(queued_text_template.format(count=outcome.cancelled_queued))
        return " ".join(part for part in parts if part).strip() or active_turn_text
    return active_turn_text


__all__ = [
    "SharedInterruptOutcome",
    "SharedInterruptState",
    "render_managed_thread_interrupt_message",
    "request_managed_thread_interrupt",
]
