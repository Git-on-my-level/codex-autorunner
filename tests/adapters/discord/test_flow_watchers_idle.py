from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.adapters.discord.flow_watchers import (
    _IDLE_BACKOFF_MAX_SECONDS,
    _IDLE_BACKOFF_STEP_SECONDS,
    PAUSE_SCAN_INTERVAL_SECONDS,
    TERMINAL_SCAN_INTERVAL_SECONDS,
    _next_idle_interval,
    _recovery_notification_is_suppressed_by_binding,
    _scan_and_enqueue_recovery_notifications,
)


def test_next_idle_interval_returns_base_for_zero_idle():
    assert _next_idle_interval(5.0, 0) == 5.0


def test_next_idle_interval_increases_with_consecutive_idle():
    base = 5.0
    assert _next_idle_interval(base, 1) == base + _IDLE_BACKOFF_STEP_SECONDS
    assert _next_idle_interval(base, 2) == base + 2 * _IDLE_BACKOFF_STEP_SECONDS
    assert _next_idle_interval(base, 3) == base + 3 * _IDLE_BACKOFF_STEP_SECONDS


def test_next_idle_interval_caps_at_maximum():
    result = _next_idle_interval(5.0, 100)
    assert result == _IDLE_BACKOFF_MAX_SECONDS


def test_next_idle_interval_with_custom_step_and_max():
    result = _next_idle_interval(2.0, 5, step=3.0, maximum=20.0)
    assert result == min(2.0 + 5 * 3.0, 20.0)
    assert result == 17.0


def test_next_idle_interval_does_not_exceed_max():
    result = _next_idle_interval(5.0, 50)
    assert result == _IDLE_BACKOFF_MAX_SECONDS
    assert result <= 30.0


def test_next_idle_interval_monotonically_increases():
    base = 5.0
    prev = base
    for i in range(20):
        current = _next_idle_interval(base, i)
        assert current >= prev
        prev = current
    assert prev == _IDLE_BACKOFF_MAX_SECONDS


@pytest.mark.anyio
async def test_pause_watcher_adaptive_backoff_intervals():
    from codex_autorunner.adapters.discord.flow_watchers import (
        watch_ticket_flow_pauses,
    )

    sleep_intervals: list[float] = []
    max_iterations = 5
    iteration = 0

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._store = MagicMock()
    service._store.list_bindings = AsyncMock(return_value=[])
    service._hub_raw_config_cache = {}

    async def fake_scan(svc: Any) -> None:
        pass

    async def capturing_sleep(interval: float) -> None:
        nonlocal iteration
        sleep_intervals.append(interval)
        iteration += 1
        if iteration >= max_iterations:
            raise asyncio.CancelledError()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers._scan_and_enqueue_pause_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers.asyncio.sleep",
            capturing_sleep,
        )
        with pytest.raises(asyncio.CancelledError):
            await watch_ticket_flow_pauses(service)

    assert len(sleep_intervals) == max_iterations
    assert (
        sleep_intervals[0] == PAUSE_SCAN_INTERVAL_SECONDS + _IDLE_BACKOFF_STEP_SECONDS
    )
    assert (
        sleep_intervals[1]
        == PAUSE_SCAN_INTERVAL_SECONDS + 2 * _IDLE_BACKOFF_STEP_SECONDS
    )
    assert (
        sleep_intervals[2]
        == PAUSE_SCAN_INTERVAL_SECONDS + 3 * _IDLE_BACKOFF_STEP_SECONDS
    )


@pytest.mark.anyio
async def test_terminal_watcher_adaptive_backoff_intervals():
    from codex_autorunner.adapters.discord.flow_watchers import (
        watch_ticket_flow_terminals,
    )

    sleep_intervals: list[float] = []
    max_iterations = 4
    iteration = 0

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._store = MagicMock()
    service._store.list_bindings = AsyncMock(return_value=[])
    service._hub_raw_config_cache = {}

    async def fake_scan(svc: Any) -> None:
        pass

    async def capturing_sleep(interval: float) -> None:
        nonlocal iteration
        sleep_intervals.append(interval)
        iteration += 1
        if iteration >= max_iterations:
            raise asyncio.CancelledError()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers._scan_and_enqueue_terminal_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers.asyncio.sleep",
            capturing_sleep,
        )
        with pytest.raises(asyncio.CancelledError):
            await watch_ticket_flow_terminals(service)

    assert len(sleep_intervals) == max_iterations
    assert (
        sleep_intervals[0]
        == TERMINAL_SCAN_INTERVAL_SECONDS + _IDLE_BACKOFF_STEP_SECONDS
    )
    assert (
        sleep_intervals[1]
        == TERMINAL_SCAN_INTERVAL_SECONDS + 2 * _IDLE_BACKOFF_STEP_SECONDS
    )


@pytest.mark.anyio
async def test_pause_watcher_resets_backoff_on_productive_scan():
    from codex_autorunner.adapters.discord.flow_watchers import (
        watch_ticket_flow_pauses,
    )

    sleep_intervals: list[float] = []
    iteration = 0

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._hub_raw_config_cache = {}

    async def fake_scan(svc: Any) -> int:
        nonlocal iteration
        iteration += 1
        return 1 if iteration >= 3 else 0

    max_sleeps = 6
    sleep_count = 0

    async def capturing_sleep(interval: float) -> None:
        nonlocal sleep_count
        sleep_intervals.append(interval)
        sleep_count += 1
        if sleep_count >= max_sleeps:
            raise asyncio.CancelledError()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers._scan_and_enqueue_pause_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.adapters.discord.flow_watchers.asyncio.sleep",
            capturing_sleep,
        )
        with pytest.raises(asyncio.CancelledError):
            await watch_ticket_flow_pauses(service)

    assert len(sleep_intervals) == max_sleeps
    assert (
        sleep_intervals[0] == PAUSE_SCAN_INTERVAL_SECONDS + _IDLE_BACKOFF_STEP_SECONDS
    )
    assert (
        sleep_intervals[1]
        == PAUSE_SCAN_INTERVAL_SECONDS + 2 * _IDLE_BACKOFF_STEP_SECONDS
    )
    assert sleep_intervals[2] == PAUSE_SCAN_INTERVAL_SECONDS
    assert sleep_intervals[3] == PAUSE_SCAN_INTERVAL_SECONDS


@pytest.mark.anyio
async def test_recovery_scan_marks_core_ledger_after_enqueue(monkeypatch) -> None:
    binding = {
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "workspace_path": "/tmp/workspace",
    }
    enqueued = []
    seen = []

    async def _enqueue(record: Any) -> None:
        enqueued.append(record)

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._store = MagicMock()
    service._store.list_bindings = AsyncMock(return_value=[binding])
    service._store.enqueue_outbox = AsyncMock(side_effect=_enqueue)
    service._hub_raw_config_cache = {}
    service._flow_run_mirror.return_value.mirror_outbound = MagicMock()

    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_sources_by_workspace",
        lambda _service: {},
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_source_for_workspace",
        lambda _service, _workspace_root: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers.list_active_ticket_flow_notification_intents",
        lambda _workspace_root: [
            SimpleNamespace(
                intent_id="intent-1",
                run_id="run-1",
                event_type="ticket_flow.commit_barrier.active",
                severity="warning",
                reason="already-seen",
                recommended_actions=(),
                cooldown_seconds=3600,
                resolved=False,
                payload={
                    "primary_state": "commit_barrier_pending",
                    "facet": {"name": "commit_barrier", "status": "active", "data": {}},
                },
                delivery_attempts={"discord:channel-1": {"status": "enqueued"}},
            ),
            SimpleNamespace(
                intent_id="intent-2",
                run_id="run-2",
                event_type="ticket_flow.commit_barrier.exhausted",
                severity="warning",
                reason="commit-barrier-retry-budget-exhausted",
                recommended_actions=("car ticket-flow status --repo /tmp/workspace",),
                cooldown_seconds=3600,
                resolved=False,
                payload={
                    "primary_state": "commit_barrier_exhausted",
                    "facet": {
                        "name": "commit_barrier",
                        "status": "exhausted",
                        "data": {},
                    },
                },
                delivery_attempts={},
            ),
        ],
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers.mark_ticket_flow_notification_intent_delivered",
        lambda _workspace_root, intent_id, **_kwargs: seen.append(intent_id),
    )

    notified = await _scan_and_enqueue_recovery_notifications(service)

    assert notified == 1
    assert len(enqueued) == 1
    assert seen == ["intent-2"]
    assert enqueued[0].record_id == "recovery:channel-1:intent-2"


@pytest.mark.anyio
async def test_recovery_scan_skips_info_status_updates(monkeypatch) -> None:
    binding = {
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "workspace_path": "/tmp/workspace",
    }

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._store = MagicMock()
    service._store.list_bindings = AsyncMock(return_value=[binding])
    service._store.enqueue_outbox = AsyncMock()
    service._hub_raw_config_cache = {}
    service._flow_run_mirror.return_value.mirror_outbound = MagicMock()

    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_sources_by_workspace",
        lambda _service: {},
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_source_for_workspace",
        lambda _service, _workspace_root: None,
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers.list_active_ticket_flow_notification_intents",
        lambda _workspace_root: [
            SimpleNamespace(
                intent_id="intent-info",
                run_id="run-1",
                event_type="ticket_flow.commit_barrier.active",
                severity="info",
                reason="done-current-ticket-has-uncommitted-worktree-changes",
                recommended_actions=("car ticket-flow status --repo /tmp/workspace",),
                cooldown_seconds=3600,
                resolved=False,
                payload={
                    "primary_state": "commit_barrier_pending",
                    "facet": {
                        "name": "commit_barrier",
                        "status": "active",
                        "data": {},
                    },
                },
                delivery_attempts={},
            )
        ],
    )

    notified = await _scan_and_enqueue_recovery_notifications(service)

    assert notified == 0
    service._store.enqueue_outbox.assert_not_awaited()


def test_discord_recovery_watchers_do_not_use_volatile_snapshot_fields() -> None:
    from codex_autorunner.adapters.discord import flow_watchers

    assert not hasattr(flow_watchers, "_recovery_fingerprint")
    assert not hasattr(flow_watchers, "_format_recovery_notification")
    source = inspect.getsource(flow_watchers)
    assert "restart_attempts" not in source
    assert "last_recovery_action" not in source


def test_recovery_notification_binding_dedupe_suppresses_same_fingerprint() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    binding = {
        "last_recovery_fingerprint": "ticket_flow_recovery:abc",
        "last_recovery_notified_at": (now - timedelta(minutes=5)).isoformat(),
    }

    assert _recovery_notification_is_suppressed_by_binding(
        binding,
        fingerprint="ticket_flow_recovery:abc",
    )


def test_recovery_notification_binding_dedupe_does_not_block_different_fingerprint() -> None:
    """A recent notify for intent A must not delay an alert for intent B (Codex #1788)."""
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    binding = {
        "last_recovery_fingerprint": "ticket_flow_recovery:abc",
        "last_recovery_notified_at": (now - timedelta(minutes=5)).isoformat(),
    }

    assert not _recovery_notification_is_suppressed_by_binding(
        binding,
        fingerprint="ticket_flow_recovery:def",
    )


@pytest.mark.anyio
async def test_recovery_scan_dedupes_issue1788_when_restart_attempts_change(
    monkeypatch,
) -> None:
    binding = {
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "workspace_path": "/tmp/workspace",
    }
    enqueued = []
    delivery_attempts: dict[str, Any] = {}
    restart_attempts = 1

    async def _enqueue(record: Any) -> None:
        enqueued.append(record)

    service = MagicMock()
    service._logger = logging.getLogger("test")
    service._store = MagicMock()
    service._store.list_bindings = AsyncMock(return_value=[binding])
    service._store.enqueue_outbox = AsyncMock(side_effect=_enqueue)
    service._hub_raw_config_cache = {}
    service._flow_run_mirror.return_value.mirror_outbound = MagicMock()

    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_sources_by_workspace",
        lambda _service: {},
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers._preferred_bound_source_for_workspace",
        lambda _service, _workspace_root: None,
    )

    def _load(_workspace_root: Any) -> list[Any]:
        return [
            SimpleNamespace(
                intent_id="stable-commit-barrier-intent",
                run_id="run-1",
                event_type="ticket_flow.commit_barrier.active",
                severity="warning",
                reason="done-current-ticket-has-uncommitted-worktree-changes",
                recommended_actions=(),
                cooldown_seconds=3600,
                resolved=False,
                payload={
                    "primary_state": "commit_barrier_pending",
                    "facet": {
                        "name": "commit_barrier",
                        "status": "active",
                        "data": {"restart_attempts": restart_attempts},
                    },
                },
                delivery_attempts=delivery_attempts,
            )
        ]

    def _mark(_workspace_root: Any, _intent_id: str, *, transport_key: str, **_: Any):
        delivery_attempts[transport_key] = {"status": "enqueued"}

    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers.list_active_ticket_flow_notification_intents",
        _load,
    )
    monkeypatch.setattr(
        "codex_autorunner.adapters.discord.flow_watchers.mark_ticket_flow_notification_intent_delivered",
        _mark,
    )

    assert await _scan_and_enqueue_recovery_notifications(service) == 1
    restart_attempts = 2
    assert await _scan_and_enqueue_recovery_notifications(service) == 0
    assert len(enqueued) == 1
