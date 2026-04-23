from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
from tests.support.discord_turn_fakes import _config, _FakeRest


@pytest.mark.asyncio
async def test_orchestrated_turn_queued_reuses_claimed_queue_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="queued", execution_id="turn-2"),
        thread=thread,
    )

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = _config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)
            self._spawn_task = asyncio.create_task
            self.claim_calls: list[tuple[str, str]] = []

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        async def _delete_channel_message_safe(
            self,
            channel_id: str,
            message_id: str,
            *,
            record_id: Optional[str] = None,
        ) -> None:
            _ = record_id
            await rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
            )

        def _claim_queued_notice_progress_message(
            self,
            *,
            channel_id: str,
            source_message_id: str,
        ) -> Optional[str]:
            self.claim_calls.append((channel_id, source_message_id))
            return "notice-1"

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        discord_message_turns_module.ManagedThreadTurnCoordinator,
        "ensure_queue_worker",
        lambda *args, **kwargs: None,
    )

    service = _Service()
    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id="m-2",
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="s1",
            orchestrator_channel_key="channel-1",
            managed_thread_surface_key=None,
            mode="pma",
            pma_enabled=True,
            execution_prompt="<user_message>\nhi\n</user_message>\n",
            public_execution_error="err",
            timeout_error="timeout",
            interrupted_error="interrupt",
            approval_mode="never",
            sandbox_policy="dangerFullAccess",
            max_actions=12,
            min_edit_interval_seconds=1.0,
            heartbeat_interval_seconds=2.0,
        )
    )

    assert result.send_final_message is False
    assert "Queued" in (result.final_message or "")
    assert service.claim_calls == [("channel-1", "m-2")]
    assert rest.channel_messages == []
    assert rest.edited_channel_messages
    assert rest.edited_channel_messages[0]["message_id"] == "notice-1"
    assert "working" in rest.edited_channel_messages[0]["payload"]["content"].lower()
    assert "queued" in rest.edited_channel_messages[-1]["payload"]["content"].lower()
