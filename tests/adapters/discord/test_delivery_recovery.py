"""Focused unit tests for the delivery recovery cursor planning seam.

Exercises :mod:`codex_autorunner.adapters.discord.delivery_recovery` directly,
covering backoff gating, cursor snapshot hashing, and unchanged-cursor
abandonment without needing the full Discord service stack.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from codex_autorunner.adapters.discord.delivery_recovery import (
    is_recovery_backoff_active,
    plan_delivery_recovery_cursor,
)

_NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


class TestIsRecoveryBackoffActive:
    def test_zero_attempts_never_in_backoff(self) -> None:
        assert (
            is_recovery_backoff_active(
                updated_at=_NOW.isoformat(),
                attempt_count=0,
                now=_NOW,
            )
            is False
        )

    def test_unparseable_updated_at_is_not_in_backoff(self) -> None:
        assert (
            is_recovery_backoff_active(
                updated_at="not-a-date",
                attempt_count=2,
                now=_NOW,
            )
            is False
        )

    def test_within_backoff_window(self) -> None:
        # attempt 1 → 5 s backoff
        updated_at = (_NOW - timedelta(seconds=1)).isoformat()
        assert (
            is_recovery_backoff_active(
                updated_at=updated_at,
                attempt_count=1,
                now=_NOW,
            )
            is True
        )

    def test_after_backoff_window_elapsed(self) -> None:
        updated_at = (_NOW - timedelta(seconds=10)).isoformat()
        assert (
            is_recovery_backoff_active(
                updated_at=updated_at,
                attempt_count=1,
                now=_NOW,
            )
            is False
        )


class TestPlanDeliveryRecoveryCursor:
    def test_returns_none_when_next_retry_in_future(self) -> None:
        cursor = {
            "_recovery": {
                "next_retry_at": (_NOW + timedelta(seconds=30)).isoformat(),
            },
            "channel_id": "123",
        }
        result, reason = plan_delivery_recovery_cursor(
            cursor=cursor,
            attempt_count=1,
            now=_NOW,
        )
        assert result is None
        assert reason is None

    def test_first_plan_writes_recovery_metadata(self) -> None:
        cursor = {"channel_id": "123", "message_id": "abc"}
        result, reason = plan_delivery_recovery_cursor(
            cursor=cursor,
            attempt_count=0,
            now=_NOW,
        )
        assert reason is None
        assert result is not None
        recovery = result["_recovery"]
        assert recovery["snapshot_hash"]
        assert recovery["unchanged_attempts"] == 1
        assert recovery["scheduled_attempt"] == 1
        assert "next_retry_at" in recovery
        assert recovery["backoff_seconds"] == 5.0
        # the original non-recovery keys are preserved
        assert result["channel_id"] == "123"
        assert result["message_id"] == "abc"

    def test_unchanged_snapshot_increments_attempts(self) -> None:
        cursor = {"channel_id": "123"}
        first, _ = plan_delivery_recovery_cursor(
            cursor=cursor,
            attempt_count=0,
            now=_NOW,
        )
        assert first is not None
        # advance past the backoff window
        later = _NOW + timedelta(seconds=100)
        second, reason = plan_delivery_recovery_cursor(
            cursor=first,
            attempt_count=1,
            now=later,
        )
        assert reason is None
        assert second is not None
        assert second["_recovery"]["unchanged_attempts"] == 2

    def test_aborts_after_max_unchanged_attempts(self) -> None:
        cursor = {"channel_id": "123"}
        now = _NOW
        attempt = 0
        current: dict | None = cursor
        for _ in range(4):
            assert current is not None
            current, reason = plan_delivery_recovery_cursor(
                cursor=current,
                attempt_count=attempt,
                now=now,
            )
            attempt += 1
            now += timedelta(seconds=400)

        # After exceeding max unchanged cursor attempts the cursor is abandoned.
        assert current is None
        assert reason == "unchanged_delivery_cursor"

    def test_changed_snapshot_resets_unchanged_attempts(self) -> None:
        cursor = {"channel_id": "123"}
        first, _ = plan_delivery_recovery_cursor(
            cursor=cursor,
            attempt_count=0,
            now=_NOW,
        )
        assert first is not None
        # mutate the payload so the snapshot hash differs
        first["channel_id"] = "456"
        later = _NOW + timedelta(seconds=100)
        second, reason = plan_delivery_recovery_cursor(
            cursor=first,
            attempt_count=1,
            now=later,
        )
        assert reason is None
        assert second is not None
        assert second["_recovery"]["unchanged_attempts"] == 1
