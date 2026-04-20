"""Tests for the Discord managed-thread delivery adapter on the durable engine.

These tests prove the Discord adapter integrates correctly with the shared
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
from codex_autorunner.integrations.discord import message_turns as discord_message_turns


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


class _DiscordServiceStub:
    def __init__(
        self,
        *,
        state_root: Path,
        send_side_effect: Optional[BaseException] = None,
        send_safe_result: bool = True,
    ) -> None:
        self._config = SimpleNamespace(root=str(state_root))
        self._send_side_effect = send_side_effect
        self._send_safe_result = send_safe_result
        self.sent_messages: list[dict[str, Any]] = []
        self.flushed_outboxes: list[dict[str, Any]] = []

    async def _send_channel_message_safe(
        self,
        channel_id: str,
        payload: dict[str, Any],
        *,
        record_id: str,
    ) -> bool:
        if self._send_side_effect is not None:
            raise self._send_side_effect
        if not self._send_safe_result:
            return False
        self.sent_messages.append(
            {
                "channel_id": channel_id,
                "payload": dict(payload),
                "record_id": record_id,
            }
        )
        return True

    def _register_discord_turn_approval_context(self, **_kwargs: Any) -> None:
        pass

    def _clear_discord_turn_approval_context(self, **_kwargs: Any) -> None:
        pass

    async def _flush_outbox_files(
        self,
        *,
        workspace_root: Path,
        channel_id: str,
    ) -> None:
        self.flushed_outboxes.append(
            {
                "workspace_root": workspace_root,
                "channel_id": channel_id,
            }
        )


def _build_hooks(
    tmp_path: Path,
    *,
    service: _DiscordServiceStub,
    channel_id: str = "channel-1",
    managed_thread_id: str = "thread-1",
) -> ManagedThreadDurableDeliveryHooks:
    hooks = discord_message_turns._build_discord_runner_hooks(
        service,
        channel_id=channel_id,
        managed_thread_id=managed_thread_id,
        workspace_root=tmp_path,
        public_execution_error="Turn failed",
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
async def test_discord_adapter_initial_delivery_marks_delivered(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert record.delivered_at is not None
    assert len(service.sent_messages) == 1
    assert service.sent_messages[0]["channel_id"] == "channel-1"
    assert "Hello from the agent" in service.sent_messages[0]["payload"]["content"]
    assert service.flushed_outboxes == [
        {"workspace_root": tmp_path, "channel_id": "channel-1"}
    ]


@pytest.mark.anyio
async def test_discord_adapter_transport_failure_leaves_record_replayable(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("network timeout"),
    )
    delivery = _build_hooks(tmp_path, service=service)
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
async def test_discord_adapter_safe_send_false_leaves_record_replayable(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(
        state_root=tmp_path,
        send_safe_result=False,
    )
    delivery = _build_hooks(tmp_path, service=service)
    finalized = _finalized_ok()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
    assert record.next_attempt_at is not None
    assert record.last_error == "discord_send_deferred:discord-queued:thread-1:turn-1"
    assert service.sent_messages == []


@pytest.mark.anyio
async def test_discord_adapter_replay_after_transient_failure(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    service_fail = _DiscordServiceStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("transient"),
    )
    delivery = _build_hooks(tmp_path, service=service_fail)
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

    service_retry = _DiscordServiceStub(state_root=tmp_path)
    delivery_retry = _build_hooks(tmp_path, service=service_retry)

    result = await delivery_retry.adapter.deliver_managed_thread_record(
        claim.record,
        claim=claim,
    )

    assert result.outcome is ManagedThreadDeliveryOutcome.DELIVERED
    assert len(service_retry.sent_messages) == 1

    updated = delivery.engine.record_attempt_result(
        failed_record.delivery_id,
        claim_token=claim.claim_token,
        result=result,
    )
    assert updated is not None
    assert updated.state is ManagedThreadDeliveryState.DELIVERED


@pytest.mark.anyio
async def test_discord_adapter_idempotent_intent_registration(
    tmp_path: Path,
) -> None:
    engine = _make_engine(tmp_path)
    finalized = _finalized_ok()
    surface = ManagedThreadSurfaceInfo(
        log_label="Discord",
        surface_kind="discord",
        surface_key="channel-1",
    )

    intent = build_managed_thread_delivery_intent(
        finalized,
        surface=surface,
        transport_target={"channel_id": "channel-1"},
    )

    reg1 = engine.create_intent(intent)
    assert reg1.inserted is True

    reg2 = engine.create_intent(intent)
    assert reg2.inserted is False
    assert reg2.record.delivery_id == reg1.record.delivery_id


@pytest.mark.anyio
async def test_discord_adapter_handles_error_status(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    finalized = _finalized_error()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(service.sent_messages) == 1
    assert "Turn failed" in service.sent_messages[0]["payload"]["content"]


@pytest.mark.anyio
async def test_discord_adapter_handles_interrupted_status(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    finalized = _finalized_interrupted()

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is None
    assert len(service.sent_messages) == 0


@pytest.mark.anyio
async def test_discord_adapter_multi_chunk_delivery(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    long_text = ("a" * 1500) + "\n" + ("b" * 1500)
    finalized = _finalized_ok(assistant_text=long_text)

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(service.sent_messages) == 2
    assert service.sent_messages[0]["record_id"].endswith(":chunk:1")
    assert service.sent_messages[1]["record_id"].endswith(":chunk:2")


@pytest.mark.anyio
async def test_discord_adapter_partial_chunk_failure_is_retryable(
    tmp_path: Path,
) -> None:
    send_call_count = 0

    class _PartialFailService(_DiscordServiceStub):
        async def _send_channel_message_safe(
            self,
            channel_id: str,
            payload: dict[str, Any],
            *,
            record_id: str,
        ) -> bool:
            nonlocal send_call_count
            send_call_count += 1
            if send_call_count > 1:
                raise RuntimeError("chunk 2 failed")
            return True

    service = _PartialFailService(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    long_text = ("a" * 1500) + "\n" + ("b" * 1500)
    finalized = _finalized_ok(assistant_text=long_text)

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.RETRY_SCHEDULED
    assert "chunk 2 failed" in (record.last_error or "")


@pytest.mark.anyio
async def test_discord_adapter_cancellation_records_retry(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(
        state_root=tmp_path,
        send_side_effect=asyncio.CancelledError(),
    )
    delivery = _build_hooks(tmp_path, service=service)
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
async def test_discord_adapter_delivered_record_is_terminal(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
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
async def test_discord_adapter_session_notice_included_in_delivery(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    delivery = _build_hooks(tmp_path, service=service)
    finalized = _finalized_ok(session_notice="A new session was started.")

    record = await handoff_managed_thread_final_delivery(
        finalized,
        delivery=delivery,
        logger=logging.getLogger("test"),
    )

    assert record is not None
    assert record.state is ManagedThreadDeliveryState.DELIVERED
    assert len(service.sent_messages) == 1
    content = service.sent_messages[0]["payload"]["content"]
    assert "A new session was started." in content
    assert "Hello from the agent" in content


@pytest.mark.anyio
async def test_discord_adapter_multiple_retries_exhaust_budget(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    engine = _make_engine(tmp_path)
    finalized = _finalized_ok()
    surface = ManagedThreadSurfaceInfo(
        log_label="Discord",
        surface_kind="discord",
        surface_key="channel-1",
    )
    intent = build_managed_thread_delivery_intent(
        finalized,
        surface=surface,
        transport_target={"channel_id": "channel-1"},
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
async def test_discord_adapter_error_status_transport_failure_is_retryable(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(
        state_root=tmp_path,
        send_side_effect=RuntimeError("error message send failed"),
    )
    delivery = _build_hooks(tmp_path, service=service)
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
async def test_discord_direct_turn_intent_before_transport_ordering(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class _OrderedService(_DiscordServiceStub):
        async def _send_channel_message_safe(
            self,
            channel_id: str,
            payload: dict[str, Any],
            *,
            record_id: str,
        ) -> None:
            events.append("transport")
            return await super()._send_channel_message_safe(
                channel_id, payload, record_id=record_id
            )

    service = _OrderedService(state_root=tmp_path)
    hooks = discord_message_turns._build_discord_runner_hooks(
        service,
        channel_id="channel-1",
        managed_thread_id="thread-1",
        workspace_root=tmp_path,
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
async def test_discord_direct_turn_error_status_uses_durable_path(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(state_root=tmp_path)
    hooks = discord_message_turns._build_discord_runner_hooks(
        service,
        channel_id="channel-1",
        managed_thread_id="thread-1",
        workspace_root=tmp_path,
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
    assert len(service.sent_messages) == 1
    assert "Turn failed" in service.sent_messages[0]["payload"]["content"]


@pytest.mark.anyio
async def test_discord_direct_turn_cancellation_leaves_durable_record(
    tmp_path: Path,
) -> None:
    service = _DiscordServiceStub(
        state_root=tmp_path,
        send_side_effect=asyncio.CancelledError(),
    )
    hooks = discord_message_turns._build_discord_runner_hooks(
        service,
        channel_id="channel-1",
        managed_thread_id="thread-1",
        workspace_root=tmp_path,
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
