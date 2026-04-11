from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import codex_autorunner.integrations.discord.message_turns as discord_message_turns_module
import codex_autorunner.integrations.discord.service as discord_service_module
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
    DiscordMessageTurnResult,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.discord_message_turns_support import (
    _config,
    _FakeGateway,
    _FakeOutboxManager,
    _FakeRest,
    _message_create,
)


class _FakeIngress:
    async def submit_message(
        self,
        request,
        *,
        resolve_paused_flow_target,
        submit_flow_reply,
        submit_thread_message,
    ):
        _ = request, resolve_paused_flow_target, submit_flow_reply
        thread_result = await submit_thread_message(request)
        return SimpleNamespace(route="thread", thread_result=thread_result)


@pytest.mark.anyio
async def test_message_event_background_failure_cleans_up_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fail_execute(*args: Any, **kwargs: Any) -> DiscordMessageTurnResult:
        _ = args, kwargs
        raise Exception("background boom")

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (
            SimpleNamespace(),
            SimpleNamespace(thread_target_id="thread-1"),
        ),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_execute_discord_thread_message",
        _fail_execute,
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        task_results = await asyncio.gather(
            *list(service._background_tasks), return_exceptions=True
        )
        assert task_results
        assert all(result is None for result in task_results)
        assert any(
            message["payload"]["content"] == "Received. Preparing turn..."
            for message in rest.channel_messages
        )
        assert any(
            message["message_id"] == "msg-1"
            for message in rest.deleted_channel_messages
        )
        assert any(
            message["payload"]["content"] == "Turn failed: background boom"
            for message in rest.channel_messages
        )
        assert service._discord_reusable_progress_messages == {}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_pre_spawn_failure_cleans_up_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_discord_thread_target",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("pre-spawn boom")),
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        assert not service._background_tasks
        assert any(
            message["payload"]["content"] == "Received. Preparing turn..."
            for message in rest.channel_messages
        )
        assert any(
            message["message_id"] == "msg-1"
            for message in rest.deleted_channel_messages
        )
        assert any(
            message["payload"]["content"] == "Turn failed: pre-spawn boom"
            for message in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_event_cleanup_failure_does_not_send_false_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_execute(*args: Any, **kwargs: Any) -> DiscordMessageTurnResult:
        _ = args, kwargs
        return DiscordMessageTurnResult(
            final_message="handled by ingress",
            preview_message_id="msg-1",
        )

    async def _fail_flush_outbox_files(*args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        raise RuntimeError("outbox boom")

    monkeypatch.setattr(
        discord_message_turns_module,
        "build_surface_orchestration_ingress",
        lambda **_: _FakeIngress(),
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "_execute_discord_thread_message",
        _fake_execute,
    )
    service._flush_outbox_files = _fail_flush_outbox_files  # type: ignore[assignment]

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("route via ingress")
        )
        assert event is not None
        await discord_message_turns_module.handle_message_event(
            service,
            event,
            build_dispatch_context(event),
            channel_id="channel-1",
            text="route via ingress",
            has_attachments=False,
            policy_result=None,
            log_event_fn=discord_service_module.log_event,
            build_ticket_flow_controller_fn=discord_service_module.build_ticket_flow_controller,
            ensure_worker_fn=discord_service_module.ensure_worker,
        )
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)
        assert any(
            message["payload"]["content"] == "handled by ingress"
            for message in rest.channel_messages
        )
        assert not any(
            message["payload"]["content"].startswith("Turn failed:")
            for message in rest.channel_messages
        )
    finally:
        await store.close()
