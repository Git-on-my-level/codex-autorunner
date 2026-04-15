from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.core.orchestration.chat_operation_ledger import (
    SQLiteChatOperationLedger,
)
from codex_autorunner.core.orchestration.chat_operation_state import (
    ChatOperationState,
)
from codex_autorunner.integrations.chat.interrupt_controller import (
    SharedInterruptState,
    request_managed_thread_interrupt,
)


class _InterruptServiceStub:
    def __init__(
        self,
        *,
        running_execution: object | None = None,
        latest_execution: object | None = None,
        stop_outcome: object | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.running_execution = running_execution
        self.latest_execution = latest_execution
        self.stop_outcome = stop_outcome
        self.stop_error = stop_error
        self.stop_calls: list[tuple[str, bool]] = []

    async def stop_thread(
        self,
        thread_target_id: str,
        *,
        cancel_queued: bool = True,
    ) -> object:
        self.stop_calls.append((thread_target_id, cancel_queued))
        if self.stop_error is not None:
            raise self.stop_error
        return self.stop_outcome or SimpleNamespace(
            interrupted_active=False,
            recovered_lost_backend=False,
            cancelled_queued=0,
            execution=None,
        )

    def get_running_execution(self, thread_target_id: str) -> object | None:
        assert thread_target_id == "thread-1"
        return self.running_execution

    def get_execution(self, thread_target_id: str, execution_id: str) -> object | None:
        assert thread_target_id == "thread-1"
        if (
            self.latest_execution is not None
            and getattr(self.latest_execution, "execution_id", None) == execution_id
        ):
            return self.latest_execution
        return None

    def get_latest_execution(self, thread_target_id: str) -> object | None:
        assert thread_target_id == "thread-1"
        return self.latest_execution


def _register_operation(
    ledger: SQLiteChatOperationLedger,
    *,
    operation_id: str,
) -> None:
    ledger.register_operation(
        operation_id=operation_id,
        surface_kind="discord",
        surface_operation_key=operation_id,
        state=ChatOperationState.RECEIVED,
    )


@pytest.mark.anyio
async def test_interrupt_controller_confirms_active_turn_and_queued_cancellation(
    tmp_path,
) -> None:
    ledger = SQLiteChatOperationLedger(tmp_path)
    _register_operation(ledger, operation_id="op-1")
    service = _InterruptServiceStub(
        running_execution=SimpleNamespace(execution_id="turn-1", status="running"),
        stop_outcome=SimpleNamespace(
            interrupted_active=True,
            recovered_lost_backend=False,
            cancelled_queued=2,
            execution=SimpleNamespace(execution_id="turn-1", status="interrupted"),
        ),
    )

    outcome = await request_managed_thread_interrupt(
        orchestration_service=service,
        thread_target_id="thread-1",
        cancel_queued=True,
        operation_store=ledger,
        operation_id="op-1",
    )

    assert outcome.state == SharedInterruptState.CONFIRMED
    assert outcome.interrupted_active is True
    assert outcome.cancelled_queued == 2
    assert service.stop_calls == [("thread-1", True)]
    stored = ledger.get_operation("op-1")
    assert stored is not None
    assert stored.state == ChatOperationState.COMPLETED
    assert stored.terminal_outcome == "confirmed"
    assert stored.metadata["interrupt_state"] == "confirmed"


@pytest.mark.anyio
async def test_interrupt_controller_returns_still_stopping_for_duplicate_request(
    tmp_path,
) -> None:
    ledger = SQLiteChatOperationLedger(tmp_path)
    _register_operation(ledger, operation_id="existing-op")
    ledger.patch_operation(
        "existing-op",
        state=ChatOperationState.INTERRUPTING,
        validate_transition=False,
        thread_target_id="thread-1",
        execution_id="turn-1",
        metadata_updates={
            "control": "interrupt",
            "interrupt_state": "requested",
            "cancel_queued": True,
            "referenced_execution_id": "turn-1",
        },
    )
    _register_operation(ledger, operation_id="op-2")
    service = _InterruptServiceStub(
        running_execution=SimpleNamespace(execution_id="turn-1", status="running")
    )

    outcome = await request_managed_thread_interrupt(
        orchestration_service=service,
        thread_target_id="thread-1",
        cancel_queued=True,
        referenced_execution_id="turn-1",
        operation_store=ledger,
        operation_id="op-2",
    )

    assert outcome.state == SharedInterruptState.STILL_STOPPING
    assert outcome.duplicate_of_operation_id == "existing-op"
    assert service.stop_calls == []
    stored = ledger.get_operation("op-2")
    assert stored is not None
    assert stored.state == ChatOperationState.INTERRUPTING
    assert stored.metadata["interrupt_state"] == "still_stopping"


@pytest.mark.anyio
async def test_interrupt_controller_returns_already_finished_for_older_turn_reference(
    tmp_path,
) -> None:
    ledger = SQLiteChatOperationLedger(tmp_path)
    _register_operation(ledger, operation_id="op-3")
    service = _InterruptServiceStub(
        running_execution=SimpleNamespace(execution_id="turn-2", status="running"),
        latest_execution=SimpleNamespace(execution_id="turn-2", status="running"),
    )

    outcome = await request_managed_thread_interrupt(
        orchestration_service=service,
        thread_target_id="thread-1",
        cancel_queued=True,
        referenced_execution_id="turn-1",
        operation_store=ledger,
        operation_id="op-3",
    )

    assert outcome.state == SharedInterruptState.ALREADY_FINISHED
    assert service.stop_calls == []
    stored = ledger.get_operation("op-3")
    assert stored is not None
    assert stored.state == ChatOperationState.COMPLETED
    assert stored.terminal_outcome == "already_finished"


@pytest.mark.anyio
async def test_interrupt_controller_marks_failed_to_dispatch_when_turn_remains_running(
    tmp_path,
) -> None:
    ledger = SQLiteChatOperationLedger(tmp_path)
    _register_operation(ledger, operation_id="op-4")
    service = _InterruptServiceStub(
        running_execution=SimpleNamespace(execution_id="turn-1", status="running"),
        stop_error=RuntimeError("backend unavailable"),
    )

    outcome = await request_managed_thread_interrupt(
        orchestration_service=service,
        thread_target_id="thread-1",
        cancel_queued=True,
        operation_store=ledger,
        operation_id="op-4",
    )

    assert outcome.state == SharedInterruptState.FAILED_TO_DISPATCH
    assert service.stop_calls == [("thread-1", True)]
    stored = ledger.get_operation("op-4")
    assert stored is not None
    assert stored.state == ChatOperationState.FAILED
    assert stored.terminal_outcome == "failed_to_dispatch"
