from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests import discord_message_turns_support as discord_support

from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    ToolCall,
)
from codex_autorunner.integrations.chat.managed_thread_progress_projector import (
    ManagedThreadProgressProjector,
)
from codex_autorunner.integrations.chat.progress_primitives import TurnProgressTracker
from codex_autorunner.integrations.telegram.notifications import (
    TelegramNotificationHandlers,
)


class _TelegramRunEventHarness(TelegramNotificationHandlers):
    def __init__(self) -> None:
        self._turn_key = ("turn-1", "thread-1")
        self._config = SimpleNamespace(
            progress_stream=SimpleNamespace(
                enabled=True,
                min_edit_interval_seconds=0.5,
                max_actions=8,
                max_output_chars=400,
            )
        )
        self._logger = logging.getLogger("test.telegram.progress_parity")
        self._turn_progress_trackers: dict[tuple[str, str], Any] = {
            self._turn_key: TurnProgressTracker(
                started_at=0.0,
                agent="codex",
                model="default",
                label="working",
                max_actions=8,
                max_output_chars=400,
            )
        }
        self._turn_progress_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_final_rendered: dict[tuple[str, str], str] = {}
        self._turn_progress_final_summary: dict[tuple[str, str], str] = {}
        self._turn_progress_updated_at: dict[tuple[str, str], float] = {}
        self._turn_progress_backoff_until: dict[tuple[str, str], float] = {}
        self._turn_progress_failure_streaks: dict[tuple[str, str], int] = {}
        self._turn_progress_suppressed_counts: dict[tuple[str, str], int] = {}
        self._turn_progress_tasks: dict[tuple[str, str], Any] = {}
        self._turn_progress_heartbeat_tasks: dict[tuple[str, str], Any] = {}
        self._turn_progress_locks: dict[tuple[str, str], Any] = {}
        self._turn_contexts: dict[tuple[str, str], Any] = {
            self._turn_key: SimpleNamespace(
                chat_id=1,
                thread_id=2,
                topic_key="topic-1",
                placeholder_message_id=100,
            )
        }
        self._cache_access: dict[str, dict[tuple[str, str], float]] = {}
        self.edits: list[tuple[tuple[str, str], str]] = []
        self.cleared: list[tuple[str, str]] = []

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[tuple[str, str]]:
        if turn_id == self._turn_key[0] and thread_id == self._turn_key[1]:
            return self._turn_key
        return None

    def _touch_cache_timestamp(self, cache_name: str, key: tuple[str, str]) -> None:
        self._cache_access.setdefault(cache_name, {})[key] = 0.0

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
        render_mode: str = "live",
    ) -> None:
        _ = (ctx, now, force)
        self.edits.append((turn_key, render_mode))

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        self.cleared.append(turn_key)


@pytest.mark.anyio
async def test_discord_and_telegram_share_semantic_phase_sequence() -> None:
    events = (
        ApprovalRequested(
            timestamp="2026-03-15T00:00:00Z",
            request_id="req-1",
            description="Need approval to run tests",
            context={},
        ),
        ToolCall(
            timestamp="2026-03-15T00:00:01Z",
            tool_name="exec",
            tool_input={"cmd": "pytest -q"},
        ),
        Completed(
            timestamp="2026-03-15T00:00:02Z",
            final_message="tests passed",
        ),
    )

    telegram = _TelegramRunEventHarness()
    telegram_projector = telegram._get_turn_progress_projector(
        telegram._turn_key,
        create=True,
    )
    assert telegram_projector is not None
    telegram_projector.mark_queued()
    telegram_projector.mark_working()

    discord_tracker = TurnProgressTracker(
        started_at=0.0,
        agent="codex",
        model="default",
        label="working",
        max_actions=8,
        max_output_chars=400,
    )
    discord_projector = ManagedThreadProgressProjector(
        discord_tracker,
        min_render_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )
    discord_projector.mark_queued()
    discord_projector.mark_working()

    async def _noop_edit_progress(**kwargs: Any) -> None:
        _ = kwargs

    for event in events:
        await telegram._apply_run_event_to_progress(telegram._turn_key, event)
        await discord_support.discord_message_turns_module._apply_discord_progress_run_event(
            discord_projector,
            event,
            edit_progress=_noop_edit_progress,
        )

    expected = ("queued", "working", "approval", "progress", "terminal")
    assert telegram_projector.phase_sequence() == expected
    assert discord_projector.phase_sequence() == expected
