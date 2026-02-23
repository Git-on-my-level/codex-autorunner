from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from codex_autorunner.integrations.app_server.threads import PMA_KEY
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.channel_messages.append(
            {"channel_id": channel_id, "payload": dict(payload)}
        )
        return {"id": f"msg-{len(self.channel_messages)}"}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return commands


class _FakeGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for event_type, payload in self._events:
            await on_dispatch(event_type, payload)
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self.stopped = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


def _config(
    root: Path,
    *,
    allowed_guild_ids: frozenset[str] = frozenset({"guild-1"}),
    allowed_channel_ids: frozenset[str] = frozenset({"channel-1"}),
    command_registration_enabled: bool = False,
    pma_enabled: bool = True,
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=allowed_guild_ids,
        allowed_channel_ids=allowed_channel_ids,
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=pma_enabled,
    )


def _bind_interaction(path: str) -> dict[str, Any]:
    return {
        "id": "inter-bind",
        "token": "token-bind",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": "bind",
                    "options": [{"type": 3, "name": "path", "value": path}],
                }
            ],
        },
    }


def _pma_interaction(subcommand: str) -> dict[str, Any]:
    return {
        "id": "inter-pma",
        "token": "token-pma",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "pma",
            "options": [{"type": 1, "name": subcommand, "options": []}],
        },
    }


def _message_create(
    content: str,
    *,
    guild_id: str = "guild-1",
    channel_id: str = "channel-1",
) -> dict[str, Any]:
    return {
        "id": "m-1",
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": [],
    }


@pytest.mark.anyio
async def test_message_create_runs_turn_for_bound_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("INTERACTION_CREATE", _bind_interaction(str(workspace))),
            ("MESSAGE_CREATE", _message_create("ship it")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["workspace_root"] == workspace.resolve()
        assert captured[0]["prompt_text"] == "ship it"
        assert captured[0]["agent"] == "codex"
        assert captured[0]["session_key"].startswith("discord:channel-1:codex:")
        assert captured[0]["orchestrator_channel_key"] == "channel-1"
        assert any(
            "Done from fake turn" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_in_pma_mode_uses_pma_session_key(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("INTERACTION_CREATE", _pma_interaction("on")),
            ("MESSAGE_CREATE", _message_create("plan next sprint")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "PMA reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["session_key"] == PMA_KEY
        assert captured[0]["orchestrator_channel_key"] == "pma:channel-1"
        assert "plan next sprint" in captured[0]["prompt_text"]
        assert captured[0]["prompt_text"] != "plan next sprint"
        assert any(
            "PMA reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_denied_by_guild_allowlist(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("hello", guild_id="x"))])
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_guild_ids=frozenset({"guild-1"}),
            allowed_channel_ids=frozenset(),
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_resumes_paused_flow_run_in_repo_mode(
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
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("needs approval"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        assert text == "needs approval"
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()
