from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_autorunner.core.orchestration import (
    ChatOperationRecoveryAction,
    ChatOperationSnapshot,
    ChatOperationState,
    SQLiteChatOperationLedger,
    initialize_orchestration_sqlite,
    plan_chat_operation_recovery,
)


def _ledger(tmp_path: Path) -> SQLiteChatOperationLedger:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return SQLiteChatOperationLedger(hub_root, durable=False)


@pytest.mark.parametrize(
    ("surface_kind", "surface_operation_key", "conversation_id"),
    (
        ("discord", "interaction-1", "conversation:discord:chan-1"),
        ("telegram", "telegram:update:1", "conversation:telegram:123:456"),
    ),
)
def test_chat_operation_ledger_round_trip_and_surface_dedupe(
    tmp_path: Path,
    surface_kind: str,
    surface_operation_key: str,
    conversation_id: str,
) -> None:
    ledger = _ledger(tmp_path)

    registration = ledger.register_operation(
        operation_id=f"{surface_kind}-op-1",
        surface_kind=surface_kind,
        surface_operation_key=surface_operation_key,
        state=ChatOperationState.RECEIVED,
        conversation_id=conversation_id,
        metadata={"surface": surface_kind},
    )
    assert registration.inserted is True
    assert registration.snapshot.state is ChatOperationState.RECEIVED

    duplicate = ledger.register_operation(
        operation_id=f"{surface_kind}-op-2",
        surface_kind=surface_kind,
        surface_operation_key=surface_operation_key,
        state=ChatOperationState.RECEIVED,
    )
    assert duplicate.inserted is False
    assert duplicate.snapshot.operation_id == f"{surface_kind}-op-1"

    updated = ledger.patch_operation(
        f"{surface_kind}-op-1",
        state=ChatOperationState.ACKNOWLEDGED,
        ack_completed_at="2026-04-15T01:02:03Z",
        delivery_state="pending",
        delivery_cursor={"operation": "send_followup", "state": "pending"},
    )
    assert updated is not None
    assert updated.state is ChatOperationState.ACKNOWLEDGED
    assert updated.delivery_state == "pending"
    assert updated.delivery_cursor == {
        "operation": "send_followup",
        "state": "pending",
    }

    by_surface = ledger.get_operation_by_surface(surface_kind, surface_operation_key)
    assert by_surface is not None
    assert by_surface.operation_id == f"{surface_kind}-op-1"


def test_chat_operation_ledger_lists_recoverable_for_thread(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="thread-op-1",
            surface_kind="telegram",
            surface_operation_key="telegram:update:1",
            thread_target_id="thread-1",
            state=ChatOperationState.RUNNING,
            created_at="2026-04-15T01:00:00Z",
            updated_at="2026-04-15T01:00:00Z",
        )
    )
    ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="thread-op-2",
            surface_kind="telegram",
            surface_operation_key="telegram:update:2",
            thread_target_id="thread-1",
            state=ChatOperationState.COMPLETED,
            created_at="2026-04-15T01:00:01Z",
            updated_at="2026-04-15T01:00:01Z",
        )
    )

    active = ledger.list_operations_for_thread("thread-1")
    assert [item.operation_id for item in active] == ["thread-op-1"]

    recoverable = ledger.list_recoverable_operations(surface_kind="telegram")
    assert [item.operation_id for item in recoverable] == ["thread-op-1"]


def test_recovery_plan_resumes_after_ack(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="recover-ack",
            surface_kind="discord",
            surface_operation_key="interaction-ack",
            state=ChatOperationState.ACKNOWLEDGED,
            ack_completed_at="2026-04-15T01:00:00Z",
            created_at="2026-04-15T01:00:00Z",
            updated_at="2026-04-15T01:00:10Z",
        )
    )

    decision = plan_chat_operation_recovery(
        snapshot,
        now=datetime(2026, 4, 15, 1, 5, 0, tzinfo=timezone.utc),
    )

    assert decision.action == ChatOperationRecoveryAction.RESUME_EXECUTION


def test_recovery_plan_replays_pending_delivery(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="recover-delivery",
            surface_kind="discord",
            surface_operation_key="interaction-delivery",
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_cursor={"operation": "send_followup", "state": "pending"},
            delivery_attempt_count=1,
            ack_completed_at="2026-04-15T01:00:00Z",
            created_at="2026-04-15T01:00:00Z",
            updated_at="2026-04-15T01:00:00Z",
        )
    )

    decision = plan_chat_operation_recovery(
        snapshot,
        now=datetime(2026, 4, 15, 1, 20, 0, tzinfo=timezone.utc),
    )

    assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY


def test_recovery_plan_expires_unacknowledged_operations(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    snapshot = ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="recover-expired",
            surface_kind="telegram",
            surface_operation_key="telegram:update:99",
            state=ChatOperationState.RECEIVED,
            created_at="2026-04-15T01:00:00Z",
            updated_at="2026-04-15T01:00:00Z",
        )
    )

    decision = plan_chat_operation_recovery(
        snapshot,
        now=datetime(2026, 4, 15, 1, 10, 0, tzinfo=timezone.utc),
        unacked_expiry=timedelta(minutes=5),
    )

    assert decision.action == ChatOperationRecoveryAction.MARK_EXPIRED


def test_patch_operation_preserves_first_visible_feedback_timestamp(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    ledger.upsert_operation(
        ChatOperationSnapshot(
            operation_id="visible-op",
            surface_kind="telegram",
            surface_operation_key="telegram:update:visible",
            state=ChatOperationState.VISIBLE,
            first_visible_feedback_at="2026-04-15T01:00:00Z",
            created_at="2026-04-15T01:00:00Z",
            updated_at="2026-04-15T01:00:00Z",
        )
    )

    updated = ledger.patch_operation(
        "visible-op",
        state=ChatOperationState.COMPLETED,
        first_visible_feedback_at="2026-04-15T01:05:00Z",
    )

    assert updated is not None
    assert updated.first_visible_feedback_at == "2026-04-15T01:00:00Z"
