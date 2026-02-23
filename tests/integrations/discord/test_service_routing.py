from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.command_sync_calls: list[dict[str, Any]] = []

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
        return {"id": "msg-1", "channel_id": channel_id, "payload": payload}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.command_sync_calls.append(
            {
                "application_id": application_id,
                "guild_id": guild_id,
                "commands": commands,
            }
        )
        return commands


class _FakeGateway:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for payload in self._events:
            await on_dispatch("INTERACTION_CREATE", payload)

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
    allow_user_ids: frozenset[str],
    command_registration_enabled: bool = True,
    command_scope: str = "guild",
    command_guild_ids: tuple[str, ...] = ("guild-1",),
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({"guild-1"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_user_ids=allow_user_ids,
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope=command_scope,
            guild_ids=command_guild_ids,
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=True,
    )


class _FailingSyncRest(_FakeRest):
    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        raise RuntimeError("simulated sync failure")


def _interaction(
    *, name: str, options: list[dict[str, Any]], user_id: str = "user-1"
) -> dict[str, Any]:
    return {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": name, "options": options}],
        },
    }


def _pma_interaction(*, name: str, user_id: str = "user-1") -> dict[str, Any]:
    return {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "pma",
            "options": [{"type": 1, "name": name, "options": []}],
        },
    }


def _normalized_interaction_event(
    *, command: str, options: dict[str, Any] | None = None, user_id: str = "user-1"
) -> ChatInteractionEvent:
    thread = ChatThreadRef(platform="discord", chat_id="channel-1", thread_id="guild-1")
    return ChatInteractionEvent(
        update_id="discord:normalized:1",
        thread=thread,
        interaction=ChatInteractionRef(thread=thread, interaction_id="inter-1"),
        from_user_id=user_id,
        payload=json.dumps(
            {
                "_discord_interaction_id": "inter-1",
                "_discord_token": "token-1",
                "command": command,
                "options": options or {},
                "guild_id": "guild-1",
            },
            separators=(",", ":"),
        ),
    )


@pytest.mark.anyio
async def test_service_enforces_allowlist_and_denies_command(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": str(tmp_path)}],
                user_id="unauthorized",
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["data"]["flags"] == 64
        assert "not authorized" in payload["data"]["content"].lower()
        assert await store.get_binding(channel_id="channel-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_then_status_updates_and_reads_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": str(workspace)}],
            ),
            _interaction(name="status", options=[]),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["workspace_path"] == str(workspace.resolve())

        assert len(rest.interaction_responses) == 2
        bind_payload = rest.interaction_responses[0]["payload"]
        status_payload = rest.interaction_responses[1]["payload"]
        assert bind_payload["data"]["flags"] == 64
        assert status_payload["data"]["flags"] == 64
        assert "bound this channel" in bind_payload["data"]["content"].lower()
        assert "channel is bound" in status_payload["data"]["content"].lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_syncs_commands_on_startup(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.command_sync_calls) == 1
        sync_call = rest.command_sync_calls[0]
        assert sync_call["application_id"] == "app-1"
        assert sync_call["guild_id"] == "guild-1"
        command_names = {cmd.get("name") for cmd in sync_call["commands"]}
        assert command_names == {"car", "pma"}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_skips_command_sync_when_disabled(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([])
    service = DiscordBotService(
        _config(
            tmp_path,
            allow_user_ids=frozenset({"user-1"}),
            command_registration_enabled=False,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert rest.command_sync_calls == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_raises_on_invalid_command_sync_config(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([])
    service = DiscordBotService(
        _config(
            tmp_path,
            allow_user_ids=frozenset({"user-1"}),
            command_registration_enabled=True,
            command_scope="guild",
            command_guild_ids=(),
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        with pytest.raises(
            ValueError, match="guild scope requires at least one guild_id"
        ):
            await service.run_forever()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_continues_when_sync_request_fails(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FailingSyncRest()
    gateway = _FakeGateway([_interaction(name="status", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert "not bound" in payload["data"]["content"].lower()
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize("subcommand", ["agent", "model"])
async def test_service_routes_car_agent_and_model_without_generic_fallback(
    tmp_path: Path, subcommand: str
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name=subcommand, options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "not bound" in content
        assert "not implemented yet for discord" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_normalized_interaction_routes_car_agent_without_generic_fallback(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        event = _normalized_interaction_event(command="car:agent")
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "not bound" in content
        assert "not implemented yet for discord" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_unknown_car_subcommand_has_explicit_unknown_message(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="mystery", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "unknown car subcommand: mystery" in content
        assert "not implemented yet for discord" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_unknown_pma_subcommand_has_explicit_unknown_message(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(name="mystery")])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "unknown pma subcommand" in content
        assert "not implemented yet for discord" not in content
    finally:
        await store.close()
