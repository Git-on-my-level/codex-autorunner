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

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryState,
    SQLiteManagedThreadDeliveryEngine,
    initialize_orchestration_sqlite,
)
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadFinalizationResult,
    ManagedThreadSurfaceInfo,
    build_managed_thread_delivery_intent,
    handoff_managed_thread_final_delivery,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    _build_telegram_runner_hooks,
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
    chat_id: int = 12345,
    thread_id: Optional[int] = 99,
    topic_key: str = "test-topic",
    public_execution_error: str = "Turn failed",
) -> ManagedThreadDurableDeliveryHooks:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    hooks = _build_telegram_runner_hooks(
        handlers,
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
) -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status="error",
        assistant_text="",
        error=error,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id="backend-1",
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
