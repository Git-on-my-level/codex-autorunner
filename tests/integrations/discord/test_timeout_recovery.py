from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests.discord_message_turns_support import _config, _FakeRest

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module


@pytest.mark.asyncio
async def test_orchestrated_turn_submission_timeout_evicts_cached_runtime_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    submit_started = asyncio.Event()

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.closed_workspaces: list[Path] = []

        async def close_workspace(self, workspace_root: Path) -> None:
            self.closed_workspaces.append(workspace_root)

    fake_supervisor = _FakeSupervisor()

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
            self._agent_runtime_supervisors = {
                ("hermes", "hermes", "m4-pma"): fake_supervisor
            }

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

    async def _hanging_submit(self, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        submit_started.set()
        await asyncio.Future()

    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_agent_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            runtime_agent_id="hermes",
            runtime_profile="m4-pma",
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        discord_message_turns_module.ManagedThreadTurnCoordinator,
        "submit_execution",
        _hanging_submit,
    )

    service = _Service()
    with pytest.raises(
        RuntimeError,
        match="Turn failed to start in time. Please retry.",
    ):
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

    assert submit_started.is_set()
    assert fake_supervisor.closed_workspaces == [tmp_path]
    assert service._agent_runtime_supervisors == {}
