from __future__ import annotations

from dataclasses import replace

import pytest

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
)
from codex_autorunner.integrations.chat.managed_thread_delivery_support import (
    ManagedThreadDeliverySendResult,
)
from codex_autorunner.integrations.chat.managed_thread_surface_kernel import (
    build_managed_thread_surface_coordinator,
    build_managed_thread_terminal_delivery_hooks,
)
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadFinalizationResult,
    ManagedThreadSurfaceInfo,
)


def _build_record() -> ManagedThreadDeliveryRecord:
    return ManagedThreadDeliveryRecord(
        delivery_id="delivery-1",
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        idempotency_key="idempotency-1",
        target=ManagedThreadDeliveryTarget(
            surface_kind="telegram",
            adapter_key="telegram",
            surface_key="topic-1",
            transport_target={"chat_id": 7, "thread_id": 8},
            metadata={"workspace_root": "/tmp/workspace"},
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text="hello",
        ),
        state=ManagedThreadDeliveryState.PENDING,
    )


def _build_finalized(status: str = "ok") -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status=status,
        assistant_text="hello",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        backend_thread_id="backend-1",
        token_usage={"output_tokens": 3},
    )


def test_surface_coordinator_projects_shared_error_config(tmp_path) -> None:
    coordinator = build_managed_thread_surface_coordinator(
        orchestration_service=object(),
        state_root=tmp_path,
        hub_client=object(),
        raw_config={"managed_thread_terminal_followup_default": True},
        surface=ManagedThreadSurfaceInfo(
            log_label="Telegram",
            surface_kind="telegram",
            surface_key="topic-1",
        ),
        public_execution_error="surface failed",
        timeout_error="surface timed out",
        interrupted_error="surface interrupted",
        timeout_seconds=90.0,
        stall_timeout_seconds=45.0,
        idle_timeout_only=True,
        logger=None,
        turn_preview="",
        preview_builder=lambda text: text[:12],
    )

    assert coordinator.surface.surface_kind == "telegram"
    assert coordinator.errors.public_execution_error == "surface failed"
    assert coordinator.errors.timeout_seconds == 90.0
    assert coordinator.errors.stall_timeout_seconds == 45.0
    assert coordinator.errors.idle_timeout_only is True


@pytest.mark.asyncio
async def test_terminal_delivery_hooks_share_intent_and_adapter_behavior(
    tmp_path,
) -> None:
    calls: list[tuple[str, str]] = []

    async def send_success(record, context):
        calls.append(("send_success", record.managed_turn_id))
        assert context.transport_target["chat_id"] == 7
        return ManagedThreadDeliverySendResult(adapter_cursor={"sent": True})

    async def send_failure(record, _context):
        calls.append(("send_failure", record.managed_turn_id))
        return ManagedThreadDeliverySendResult()

    async def cleanup(record, context):
        calls.append(("cleanup", record.managed_turn_id))
        assert context.metadata["workspace_root"] == "/tmp/workspace"

    surface = ManagedThreadSurfaceInfo(
        log_label="Telegram",
        surface_kind="telegram",
        surface_key="topic-1",
        metadata={"parse_mode": "HTML"},
    )
    hooks = build_managed_thread_terminal_delivery_hooks(
        state_root=tmp_path,
        surface=surface,
        adapter_key="telegram",
        transport_target={"chat_id": 7, "thread_id": 8},
        metadata={"workspace_root": "/tmp/workspace"},
        send_success=send_success,
        send_failure=send_failure,
        cleanup=cleanup,
    )

    finalized = _build_finalized()
    intent = hooks.build_delivery_intent(finalized)

    assert intent is not None
    assert intent.target.surface_kind == "telegram"
    assert intent.target.transport_target == {"chat_id": 7, "thread_id": 8}
    assert intent.target.metadata == {"parse_mode": "HTML"}
    assert intent.metadata == {"workspace_root": "/tmp/workspace"}
    assert intent.envelope.metadata == {"workspace_root": "/tmp/workspace"}
    assert hooks.build_delivery_intent(replace(finalized, status="interrupted")) is None

    result = await hooks.adapter.deliver_managed_thread_record(
        _build_record(),
        claim=object(),
    )

    assert result.outcome == ManagedThreadDeliveryOutcome.DELIVERED
    assert result.adapter_cursor == {"sent": True}
    assert calls == [("send_success", "turn-1"), ("cleanup", "turn-1")]
