from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.integrations.discord.flow_watchers import (
    _IDLE_BACKOFF_MAX_SECONDS,
    _IDLE_BACKOFF_STEP_SECONDS,
    PAUSE_SCAN_INTERVAL_SECONDS,
    TERMINAL_SCAN_INTERVAL_SECONDS,
    _next_idle_interval,
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
    from codex_autorunner.integrations.discord.flow_watchers import (
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
            "codex_autorunner.integrations.discord.flow_watchers._scan_and_enqueue_pause_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.integrations.discord.flow_watchers.asyncio.sleep",
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
    from codex_autorunner.integrations.discord.flow_watchers import (
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
            "codex_autorunner.integrations.discord.flow_watchers._scan_and_enqueue_terminal_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.integrations.discord.flow_watchers.asyncio.sleep",
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
    from codex_autorunner.integrations.discord.flow_watchers import (
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
            "codex_autorunner.integrations.discord.flow_watchers._scan_and_enqueue_pause_notifications",
            fake_scan,
        )
        mp.setattr(
            "codex_autorunner.integrations.discord.flow_watchers.asyncio.sleep",
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
