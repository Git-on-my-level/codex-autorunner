from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.integrations.discord import (
    message_turns as discord_message_turns_module,
)
from codex_autorunner.integrations.discord.message_turns import (
    DiscordMessageTurnResult,
)


@pytest.mark.asyncio
async def test_run_agent_turn_for_message_forwards_delivery_suppression(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_discord_orchestrated_turn_for_message(
        *args: Any, **kwargs: Any
    ) -> DiscordMessageTurnResult:
        _ = args
        captured.update(kwargs)
        return DiscordMessageTurnResult(final_message="ok")

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    service = SimpleNamespace(
        _store=_Store(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_run_discord_orchestrated_turn_for_message",
        _fake_run_discord_orchestrated_turn_for_message,
    )

    result = await discord_message_turns_module.run_agent_turn_for_message(
        service,
        workspace_root=tmp_path,
        prompt_text="hello",
        input_items=None,
        agent="codex",
        model_override=None,
        reasoning_effort=None,
        session_key="session-1",
        orchestrator_channel_key="channel-1",
        suppress_managed_thread_delivery=True,
        max_actions=1,
        min_edit_interval_seconds=0.1,
        heartbeat_interval_seconds=0.1,
        log_event_fn=lambda *_args, **_kwargs: None,
    )

    assert result.final_message == "ok"
    assert captured["suppress_managed_thread_delivery"] is True
