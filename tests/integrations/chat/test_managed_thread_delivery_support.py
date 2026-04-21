"""Tests for shared managed-thread terminal delivery wrapper."""

from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
)
from codex_autorunner.integrations.chat.managed_thread_delivery_support import (
    ManagedThreadDeliveryCleanupContext,
    deliver_managed_thread_terminal_record,
)


def _ok_record() -> ManagedThreadDeliveryRecord:
    return ManagedThreadDeliveryRecord(
        delivery_id="d-1",
        managed_thread_id="mt-1",
        managed_turn_id="t-1",
        idempotency_key="id-1",
        target=ManagedThreadDeliveryTarget(
            surface_kind="discord",
            adapter_key="discord",
            surface_key="c1",
            transport_target={},
            metadata={"workspace_root": "/tmp"},
        ),
        envelope=ManagedThreadDeliveryEnvelope(
            envelope_version="managed_thread_delivery.v1",
            final_status="ok",
            assistant_text="hi",
        ),
        state=ManagedThreadDeliveryState.PENDING,
    )


@pytest.mark.asyncio
async def test_cleanup_failure_after_send_still_delivers() -> None:
    """Post-send cleanup must not fail the attempt (avoids duplicate retries)."""

    async def send_ok(_ctx: ManagedThreadDeliveryCleanupContext):
        return None

    async def cleanup_raises(_ctx: ManagedThreadDeliveryCleanupContext) -> None:
        raise RuntimeError("flush exploded")

    result = await deliver_managed_thread_terminal_record(
        _ok_record(),
        send_success=send_ok,
        send_failure=send_ok,
        cleanup=cleanup_raises,
        cleanup_statuses=frozenset({"ok"}),
    )
    assert isinstance(result, ManagedThreadDeliveryAttemptResult)
    assert result.outcome == ManagedThreadDeliveryOutcome.DELIVERED
    assert result.error is None
