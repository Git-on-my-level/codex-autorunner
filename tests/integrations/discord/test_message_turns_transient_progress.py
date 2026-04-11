import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tests import discord_message_turns_support as support

from codex_autorunner.integrations.discord.errors import DiscordTransientError

pytestmark = pytest.mark.slow


class _TransientEditProgressRest(support._FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _ = channel_id, message_id, payload
        self.edit_attempts += 1
        raise DiscordTransientError("simulated transient progress edit failure")


@pytest.mark.anyio
async def test_reconcile_progress_lease_retries_when_retire_edit_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = support.DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_turn_progress_lease(
        lease_id="lease-1",
        managed_thread_id="thread-1",
        execution_id="exec-1",
        channel_id="channel-1",
        message_id="msg-1",
        state="active",
        progress_label="running",
    )

    fake_orchestration_service = SimpleNamespace(
        get_thread_target=lambda _thread_id: SimpleNamespace(
            thread_target_id="thread-1"
        ),
        get_latest_execution=lambda _thread_id: SimpleNamespace(
            execution_id="exec-1",
            status="ok",
        ),
        get_running_execution=lambda _thread_id: None,
        get_execution=lambda _thread_id, _execution_id: SimpleNamespace(
            execution_id="exec-1",
            status="ok",
        ),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "build_discord_thread_orchestration_service",
        lambda _service: fake_orchestration_service,
    )

    service = SimpleNamespace(
        _store=store,
        _rest=support._EditFailingProgressRest(),
        _config=support._config(tmp_path),
        _logger=logging.getLogger("test"),
    )

    try:
        reconciled = await support.discord_message_turns_module.reconcile_discord_turn_progress_leases(
            service,
            lease_id="lease-1",
        )
        assert reconciled == 0

        retained = await store.get_turn_progress_lease(lease_id="lease-1")
        assert retained is not None
        assert retained.state == "retiring"

        service._rest = support._FakeRest()
        reconciled = await support.discord_message_turns_module.reconcile_discord_turn_progress_leases(
            service,
            lease_id="lease-1",
        )
        assert reconciled == 1

        retired = await store.get_turn_progress_lease(lease_id="lease-1")
        assert retired is None
        assert service._rest.edited_channel_messages == [
            {
                "channel_id": "channel-1",
                "message_id": "msg-1",
                "payload": {
                    "content": "Status: this turn already completed.",
                    "components": [],
                },
            }
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_orchestrated_turn_interrupt_send_falls_back_when_progress_ack_edit_is_transient(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    class _Rest(support._FakeRest):
        async def edit_channel_message(
            self, *, channel_id: str, message_id: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            _ = channel_id, message_id, payload
            raise DiscordTransientError("transient edit failure")

    rest = _Rest()

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

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
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        _ = args, kwargs
        return {"status": "interrupted", "error": "Discord PMA turn interrupted"}

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return SimpleNamespace(finalized=await _fake_finalize())

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    service = _Service()
    support.discord_message_turns_module.request_discord_turn_progress_reuse(
        service,
        thread_target_id="thread-1",
        source_message_id="m-2",
        acknowledgement="Message received. Switching to it now...",
    )

    result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
        service,
        workspace_root=tmp_path,
        prompt_text="first prompt",
        source_message_id="m-1",
        agent="codex",
        model_override=None,
        reasoning_effort=None,
        session_key="session-1",
        orchestrator_channel_key="channel-1",
        managed_thread_surface_key=None,
        mode="pma",
        pma_enabled=True,
        execution_prompt="<user_message>\nfirst prompt\n</user_message>\n",
        public_execution_error="Discord PMA turn failed",
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        approval_mode="never",
        sandbox_policy="dangerFullAccess",
        max_actions=12,
        min_edit_interval_seconds=1.0,
        heartbeat_interval_seconds=2.0,
    )

    assert result.send_final_message is True
    assert result.final_message == "Message received. Switching to it now..."
    assert len(rest.channel_messages) == 1
    assert service._discord_turn_progress_reuse_requests == {}
    assert service._discord_reusable_progress_messages == {}


@pytest.mark.asyncio
async def test_orchestrated_turn_ignores_transient_progress_edit_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    rest = _TransientEditProgressRest()
    thread = SimpleNamespace(thread_target_id="thread-1")
    started_execution = SimpleNamespace(
        execution=SimpleNamespace(status="running"),
        thread=thread,
    )

    class _Store:
        async def get_binding(self, *, channel_id: str) -> dict[str, Any]:
            assert channel_id == "channel-1"
            return {}

    class _Service:
        def __init__(self) -> None:
            self._config = support._config(tmp_path)
            self._store = _Store()
            self._rest = rest
            self._logger = logging.getLogger(__name__)

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
            return "codex", None

        def _runtime_agent_for_binding(self, binding: Any) -> str:
            _ = binding
            return "codex"

    async def _fake_begin(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return started_execution

    async def _fake_complete(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        await asyncio.sleep(0.03)
        return SimpleNamespace(
            finalized={
                "status": "ok",
                "assistant_text": "done",
                "token_usage": None,
            }
        )

    monkeypatch.setattr(
        support.discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (SimpleNamespace(), thread),
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        support.discord_message_turns_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()

    def _capture_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        loop_errors.append(context)

    loop.set_exception_handler(_capture_exception)
    try:
        result = await support.discord_message_turns_module._run_discord_orchestrated_turn_for_message(
            _Service(),
            workspace_root=tmp_path,
            prompt_text="hi",
            input_items=None,
            source_message_id=None,
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
            min_edit_interval_seconds=0.0,
            heartbeat_interval_seconds=0.01,
        )
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert result.final_message == "done"
    assert rest.edit_attempts >= 1
    assert loop_errors == []
