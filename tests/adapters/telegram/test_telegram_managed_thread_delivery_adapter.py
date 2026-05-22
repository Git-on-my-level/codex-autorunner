"""Tests for the Telegram managed-thread delivery adapter on the durable engine.

These tests prove the Telegram adapter integrates correctly with the shared
durable delivery engine: initial delivery, transient failure, replay,
idempotency, and terminal handling.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.adapters.chat.managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadFinalizationResult,
    ManagedThreadSurfaceInfo,
    build_managed_thread_delivery_intent,
    handoff_managed_thread_final_delivery,
)
from codex_autorunner.adapters.telegram.handlers.commands.execution import (
    _build_telegram_runner_hooks,
)
from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    ManagedThreadFailureRecoverySummary,
    SQLiteManagedThreadDeliveryEngine,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.turn_assistant_output import (
    TurnAssistantOutput,
)


def _make_engine(
    tmp_path: Path,
    *,
    retry_backoff_seconds: int = 0,
    max_attempts: int = 5,
) -> SQLiteManagedThreadDeliveryEngine:
    from datetime import timedelta

    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return SQLiteManagedThreadDeliveryEngine(
        hub_root,
        durable=False,
        retry_backoff=timedelta(seconds=retry_backoff_seconds),
        max_attempts=max_attempts,
    )


class _TelegramHandlersStub:
    def __init__(
        self,
        *,
        state_root: Path,
        send_side_effect: Optional[BaseException] = None,
    ) -> None:
        self._config = SimpleNamespace(root=str(state_root))
        self._send_side_effect = send_side_effect
        self._logger = logging.getLogger("test.telegram.adapter")
        self.sent_messages: list[dict[str, Any]] = []
        self.flushed_outbox: list[dict[str, Any]] = []
        self.outbox_messages: list[dict[str, Any]] = []

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
    ) -> None:
        if self._send_side_effect is not None:
            raise self._send_side_effect
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to": reply_to,
            }
        )

    async def _send_message_with_outbox(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int],
        reply_to: Optional[int],
        record_id: Optional[str] = None,
        outbox_key: Optional[str] = None,
        delivery_metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        self.outbox_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to": reply_to,
                "record_id": record_id,
                "outbox_key": outbox_key,
                "delivery_metadata": dict(delivery_metadata or {}),
            }
        )
        await self._send_message(
            chat_id,
            text,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        return True

    async def _flush_outbox_files(
        self,
        record: Any,
        *,
        chat_id: int,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        topic_key: str = "",
    ) -> None:
        self.flushed_outbox.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "topic_key": topic_key,
                "workspace_path": getattr(record, "workspace_path", None),
                "pma_enabled": getattr(record, "pma_enabled", False),
            }
        )


def _build_hooks(
    tmp_path: Path,
    *,
    handlers: _TelegramHandlersStub,
    managed_thread_id: str = "thread-1",
    chat_id: int = 12345,
    thread_id: Optional[int] = 99,
    topic_key: str = "test-topic",
    public_execution_error: str = "Turn failed",
) -> ManagedThreadDurableDeliveryHooks:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    hooks = _build_telegram_runner_hooks(
        handlers,
        managed_thread_id=managed_thread_id,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_key=topic_key,
        public_execution_error=public_execution_error,
    )
    assert hooks.durable_delivery is not None
    return hooks.durable_delivery


def _finalized_ok(
    *,
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
    assistant_text: str = "Hello from the agent",
    session_notice: Optional[str] = None,
) -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id="backend-1",
        session_notice=session_notice,
    )


def _finalized_error(
    *,
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
    error: str = "Something went wrong",
    failure_recovery: Any = None,
) -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status="error",
        assistant_text="",
        error=error,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id="backend-1",
        failure_recovery=failure_recovery,
    )


def _finalized_interrupted(
    *,
    managed_thread_id: str = "thread-1",
    managed_turn_id: str = "turn-1",
) -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status="interrupted",
        assistant_text="",
        error=None,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id="backend-1",
    )


@pytest.mark.anyio
async def test_telegram_adapter_initial_delivery_marks_delivered(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert record.delivered_at is not None
    assert len(handlers.sent_messages) == 1
    assert handlers.sent_messages[0]["chat_id"] == 12345
    assert "Hello from the agent" in handlers.sent_messages[0]["text"]
    assert len(handlers.flushed_outbox) == 1


@pytest.mark.anyio
async def test_telegram_adapter_success_delivery_uses_sealed_output_text(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    record = ManagedThreadDeliveryRecord(
        delivery_id="delivery-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        idempotency_key="idem-1",
        target=ManagedThreadDeliveryTarget(
            surface_kind="telegram",
            adapter_key="telegram",
            surface_key="test-topic",
            transport_target={"chat_id": 12345, "thread_id": 99},
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text="User: q1\nAssistant: stale transcript",
            assistant_output=TurnAssistantOutput(
                managed_thread_id="thread-1",
                managed_turn_id="turn-1",
                backend_thread_id="backend-1",
                backend_turn_id="backend-turn-1",
                text="Current turn answer",
                ownership="trimmed_from_cumulative",
                source="reducer",
            ),
        ),
        state=ManagedThreadDeliveryState.CLAIMED,
    )

    result = await delivery.adapter.deliver_managed_thread_record(
        record,
        claim=SimpleNamespace(),
    )

    assert result.outcome is ManagedThreadDeliveryOutcome.DELIVERED
    assert len(handlers.sent_messages) == 1
    assert "Current turn answer" in handlers.sent_messages[0]["text"]
    assert "stale transcript" not in handlers.sent_messages[0]["text"]
    metadata = handlers.outbox_messages[0]["delivery_metadata"]
    assert metadata["turn_output_ownership"] == "trimmed_from_cumulative"
    assert metadata["turn_output_source"] == "reducer"


@pytest.mark.anyio
async def test_telegram_adapter_transport_failure_leaves_record_replayable(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("network timeout"),
    )
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
    assert record.next_attempt_at is not None
    assert record.last_error is not None
    assert "network timeout" in record.last_error


@pytest.mark.anyio
async def test_telegram_adapter_replay_after_transient_failure(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    handlers_fail = _TelegramHandlersStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("transient"),
    )
    delivery = _build_hooks(tmp_path, handlers=handlers_fail)
    finalized = _finalized_ok()

    failed_record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )
    assert failed_record is not None
    assert failed_record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED

    future_now = datetime.now(timezone.utc) + timedelta(hours=1)
    claim = delivery.engine.claim_delivery(
        failed_record.delivery_id,
        now=future_now,
    )
    assert claim is not None

    handlers_retry = _TelegramHandlersStub(state_root=tmp_path)
    delivery_retry = _build_hooks(tmp_path, handlers=handlers_retry)

    result = await delivery_retry.adapter.deliver_managed_thread_record(
        claim.record,
        claim=claim,
    )

    assert result.outcome is ManagedThreadDeliveryOutcome.DELIVERED
    assert len(handlers_retry.sent_messages) == 1

    updated = delivery.engine.record_attempt_result(
        failed_record.delivery_id,
        claim_token=claim.claim_token,
        result=result,
    )
    assert updated is not None
    assert updated.state is ManagedThreadDeliveryState.DELIVERED


@pytest.mark.anyio
async def test_telegram_adapter_idempotent_intent_registration(
    tmp_path: Path,
) -> None:
    engine = _make_engine(tmp_path)
    finalized = _finalized_ok()
    surface = ManagedThreadSurfaceInfo(
        log_label="Telegram",
        surface_kind="telegram",
        surface_key="test-topic",
    )

    intent = build_managed_thread_delivery_intent(
        finalized,
        surface=surface,
        transport_target={"chat_id": 12345, "thread_id": 99},
    )

    reg1 = engine.create_intent(intent)
    assert reg1.inserted is True

    reg2 = engine.create_intent(intent)
    assert reg2.inserted is False
    assert reg2.record.delivery_id == reg1.record.delivery_id


@pytest.mark.anyio
async def test_telegram_adapter_handles_error_status(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_error()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(handlers.sent_messages) == 1
    assert "Turn failed" in handlers.sent_messages[0]["text"]


@pytest.mark.anyio
async def test_telegram_adapter_renders_failed_turn_recovery_summary(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_error(
        error="App-server disconnected",
        failure_recovery=ManagedThreadFailureRecoverySummary(
            failure_kind="app_server_disconnected",
            error_text="App-server disconnected",
            recovered_assistant_tail="partial assistant output",
            recovered_notice_tail="latest status",
            trace_manifest_id="trace-1",
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
            managed_turn_id="turn-1",
        ),
    )

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    text = handlers.sent_messages[0]["text"]
    assert "Turn failed: App-server disconnected" in text
    assert "Failure kind: app_server_disconnected" in text
    assert "Trace: trace-1" in text
    assert "latest status" in text
    assert "partial assistant output" in text


@pytest.mark.anyio
async def test_telegram_adapter_handles_interrupted_status(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_interrupted()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is None
    assert len(handlers.sent_messages) == 0


@pytest.mark.anyio
async def test_telegram_adapter_cancellation_records_retry(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(
        state_root=tmp_path,
        send_side_effect=asyncio.CancelledError(),
    )
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok()

    with pytest.raises(asyncio.CancelledError):
        await handoff_managed_thread_final_delivery(
            finalized,
            delivery=delivery,
            logger=logging.getLogger("test"),
        )

    engine = delivery.engine
    persisted = engine._ledger.get_delivery_by_idempotency_key(
        delivery.build_delivery_intent(finalized).idempotency_key,
    )
    assert persisted is not None
    assert persisted.state is ManagedThreadDeliveryState.RETRY_SCHEDULED


@pytest.mark.anyio
async def test_telegram_adapter_delivered_record_is_terminal(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )
    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED

    engine = delivery.engine
    re_claim = engine.claim_delivery(record.delivery_id)
    assert re_claim is None


@pytest.mark.anyio
async def test_telegram_second_handoff_after_delivered_does_not_resend(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok()

    first = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )
    second = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert first is not None
    assert second is not None
    assert first.delivery_id == second.delivery_id
    assert second.state is ManagedThreadDeliveryState.DELIVERED
    assert len(handlers.sent_messages) == 1
    assert len(handlers.outbox_messages) == 1
    assert handlers.outbox_messages[0]["outbox_key"] == first.idempotency_key


@pytest.mark.anyio
async def test_telegram_adapter_session_notice_included_in_delivery(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_ok(session_notice="A new session was started.")

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(handlers.sent_messages) == 1
    text = handlers.sent_messages[0]["text"]
    assert "A new session was started." in text
    assert "Hello from the agent" in text


@pytest.mark.anyio
async def test_telegram_adapter_multiple_retries_exhaust_budget(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    engine = _make_engine(tmp_path)
    finalized = _finalized_ok()
    surface = ManagedThreadSurfaceInfo(
        log_label="Telegram",
        surface_kind="telegram",
        surface_key="test-topic",
    )
    intent = build_managed_thread_delivery_intent(
        finalized,
        surface=surface,
        transport_target={"chat_id": 12345, "thread_id": 99},
    )
    reg = engine.create_intent(intent)
    assert reg.inserted

    base_now = datetime.now(timezone.utc)
    for attempt in range(5):
        future_now = base_now + timedelta(hours=1 + attempt)
        claim = engine.claim_delivery(reg.record.delivery_id, now=future_now)
        if claim is None:
            break
        engine.record_attempt_result(
            reg.record.delivery_id,
            claim_token=claim.claim_token,
            result=ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=f"attempt {attempt + 1} failed",
            ),
        )

    final_record = engine._ledger.get_delivery(reg.record.delivery_id)
    assert final_record is not None
    assert final_record.state is ManagedThreadDeliveryState.FAILED
    assert final_record.attempt_count >= 5


@pytest.mark.anyio
async def test_telegram_adapter_error_status_transport_failure_is_retryable(
    tmp_path: Path,
) -> None:
    handlers = _TelegramHandlersStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("error message send failed"),
    )
    delivery = _build_hooks(tmp_path, handlers=handlers)
    finalized = _finalized_error()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
    assert "error message send failed" in (record.last_error or "")


@pytest.mark.anyio
async def test_telegram_direct_turn_intent_before_transport_ordering(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class _OrderedHandlers(_TelegramHandlersStub):
        async def _send_message(
            self,
            chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
        ) -> None:
            events.append("transport")
            return await super()._send_message(
                chat_id, text, thread_id=thread_id, reply_to=reply_to
            )

    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    handlers = _OrderedHandlers(state_root=tmp_path)
    hooks = _build_telegram_runner_hooks(
        handlers,
        managed_thread_id="thread-1",
        chat_id=12345,
        thread_id=99,
        topic_key="test-topic",
        public_execution_error="Turn failed",
    )
    assert hooks.durable_delivery is not None

    original_create_intent = hooks.durable_delivery.engine.create_intent

    def _tracked_create_intent(intent: Any) -> Any:
        events.append("intent_created")
        return original_create_intent(intent)

    hooks.durable_delivery.engine.create_intent = _tracked_create_intent

    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=hooks.durable_delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert events.index("intent_created") < events.index("transport")


@pytest.mark.anyio
async def test_telegram_direct_turn_error_status_uses_durable_path(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    handlers = _TelegramHandlersStub(state_root=tmp_path)
    hooks = _build_telegram_runner_hooks(
        handlers,
        managed_thread_id="thread-1",
        chat_id=12345,
        thread_id=99,
        topic_key="test-topic",
        public_execution_error="Turn failed",
    )
    assert hooks.durable_delivery is not None
    finalized = _finalized_error()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=hooks.durable_delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(handlers.sent_messages) == 1
    assert "Turn failed" in handlers.sent_messages[0]["text"]


@pytest.mark.anyio
async def test_telegram_direct_turn_cancellation_leaves_durable_record(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    handlers = _TelegramHandlersStub(
        state_root=tmp_path,
        send_side_effect=asyncio.CancelledError(),
    )
    hooks = _build_telegram_runner_hooks(
        handlers,
        managed_thread_id="thread-1",
        chat_id=12345,
        thread_id=99,
        topic_key="test-topic",
        public_execution_error="Turn failed",
    )
    assert hooks.durable_delivery is not None
    finalized = _finalized_ok()

    with pytest.raises(asyncio.CancelledError):
        await handoff_managed_thread_final_delivery(
            finalized,
            delivery=hooks.durable_delivery,
            logger=logging.getLogger("test"),
        )

    persisted = hooks.durable_delivery.engine._ledger.get_delivery_by_idempotency_key(
        hooks.durable_delivery.build_delivery_intent(finalized).idempotency_key,
    )
    assert persisted is not None
    assert persisted.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
