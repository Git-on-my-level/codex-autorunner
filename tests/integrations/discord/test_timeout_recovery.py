from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests.discord_message_turns_support import _config, _FakeRest

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module


@pytest.mark.asyncio
async def test_discord_pma_turn_uses_pma_submission_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    captured_timeout: Optional[float] = 123.0

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
            self._agent_runtime_supervisors = {}

        async def _send_channel_message(
            self, channel_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            return await rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        def _register_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _clear_discord_turn_approval_context(self, **kwargs: Any) -> None:
            _ = kwargs

        def _resolve_agent_state(self, binding: Any) -> tuple[str, Optional[str]]:
            _ = binding
            return "hermes", "m4-pma"

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "hermes"

    async def _fake_run_managed_surface_turn(request: Any, *, config: Any) -> Any:
        nonlocal captured_timeout
        _ = request
        captured_timeout = config.submission_timeout_seconds
        return SimpleNamespace(
            final_message="ok",
            preview_message_id=None,
            execution_id=None,
            intermediate_message=None,
            token_usage=None,
            elapsed_seconds=0.0,
            send_final_message=True,
            delivery_visibility_pending=False,
            durable_delivery_id=None,
            durable_delivery_claim_token=None,
            deferred_delivery=False,
            preserve_progress_lease=False,
        )

    monkeypatch.setattr(
        discord_message_turns_module,
        "DISCORD_PMA_SUBMISSION_TIMEOUT_SECONDS",
        123.0,
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "run_managed_surface_turn",
        _fake_run_managed_surface_turn,
    )

    service = _Service()
    result = (
        await discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            service,
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id=None,
            agent="hermes",
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

    assert result.final_message == "ok"
    assert captured_timeout == 123.0
