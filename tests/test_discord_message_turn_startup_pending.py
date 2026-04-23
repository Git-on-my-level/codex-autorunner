from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.discord import (
    message_turns as discord_message_turns_module,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.support.discord_turn_fakes import (
    _config,
    _FakeOutboxManager,
    _FakeRest,
    _message_create,
)


@pytest.mark.anyio
async def test_message_create_startup_failure_keeps_generic_error_without_raw_detail(
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
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    submit_started = asyncio.Event()

    async def _hanging_submit(self, *args, **kwargs):
        _ = args, kwargs
        submit_started.set()
        await asyncio.Future()

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
        "DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        discord_message_turns_module.ManagedThreadTurnCoordinator,
        "submit_execution",
        _hanging_submit,
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("please continue")
        )
        assert event is not None
        await asyncio.wait_for(
            service._handle_message_event(event, build_dispatch_context(event)),
            timeout=3,
        )
        await asyncio.wait_for(submit_started.wait(), timeout=1)
        task = next(
            pending
            for pending in service._background_tasks
            if isinstance(
                getattr(pending, "_discord_progress_task_context", None),
                dict,
            )
            and getattr(pending, "_discord_progress_task_context", {}).get(
                "failure_note"
            )
        )
        with contextlib.suppress(RuntimeError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert submit_started.is_set()
        assert rest.edited_channel_messages
        assert any(
            "Turn failed to start in time. Please retry."
            in item["payload"].get("content", "")
            for item in rest.edited_channel_messages
        )
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert not any("Turn failed:" in content for content in contents)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_reconciles_cancelled_background_startup_after_execution_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._resolve_agent_state = lambda binding: ("hermes", None)  # type: ignore[method-assign]

    startup_entered = asyncio.Event()

    class _HangingHarness:
        def supports(self, _capability: str) -> bool:
            return True

        async def ensure_ready(self, _workspace_root: Path) -> None:
            startup_entered.set()
            await asyncio.Future()

    descriptor = AgentDescriptor(
        id="hermes",
        name="Hermes",
        capabilities=("durable_threads", "message_turns", "interrupt"),
        make_harness=lambda _ctx: _HangingHarness(),
        healthcheck=lambda: True,
        runtime_kind="hermes",
    )
    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        lambda context=None: {"hermes": descriptor},
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "get_registered_agents",
        lambda context=None: {"hermes": descriptor},
    )
    monkeypatch.setattr(
        discord_message_turns_module,
        "resolve_agent_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            logical_agent_id="hermes",
            logical_profile=None,
            resolution_kind="passthrough",
            runtime_agent_id="hermes",
            runtime_profile=None,
        ),
    )

    try:
        event = service._chat_adapter.parse_message_event(
            _message_create("please continue")
        )
        assert event is not None
        await asyncio.wait_for(
            service._handle_message_event(event, build_dispatch_context(event)),
            timeout=3,
        )
        await asyncio.wait_for(startup_entered.wait(), timeout=1)
        task = next(
            pending
            for pending in service._background_tasks
            if isinstance(
                getattr(pending, "_discord_progress_task_context", None),
                dict,
            )
            and getattr(pending, "_discord_progress_task_context", {}).get(
                "failure_note"
            )
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        deadline = asyncio.get_running_loop().time() + 1.0
        while not rest.edited_channel_messages:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        orch = discord_message_turns_module.build_discord_thread_orchestration_service(
            service
        )
        binding = orch.get_binding(surface_kind="discord", surface_key="channel-1")
        assert binding is not None
        leases = []
        while True:
            assert asyncio.get_running_loop().time() < deadline
            leases = await store.list_turn_progress_leases(
                managed_thread_id=binding.thread_target_id
            )
            if not leases:
                break
            await asyncio.sleep(0.01)
        assert not leases
        assert rest.edited_channel_messages
        edited_contents = [
            str(item["payload"].get("content", ""))
            for item in rest.edited_channel_messages
        ]
        assert any(
            "background task terminated unexpectedly" in content.lower()
            for content in edited_contents
        ), edited_contents
    finally:
        for pending in list(service._background_tasks):
            pending.cancel()
        for pending in list(service._background_tasks):
            with contextlib.suppress(BaseException):
                await pending
        await store.close()
