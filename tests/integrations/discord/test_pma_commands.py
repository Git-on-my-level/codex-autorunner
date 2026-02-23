from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.pma_sink import PmaActiveSinkStore
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
    root: Path, *, allow_user_ids: frozenset[str], pma_enabled: bool = True
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
            enabled=True,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        pma_enabled=pma_enabled,
    )


def _pma_interaction(*, subcommand: str, user_id: str = "user-1") -> dict[str, Any]:
    return {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "pma",
            "options": [{"type": 1, "name": subcommand, "options": []}],
        },
    }


def _bind_interaction(*, path: str, user_id: str = "user-1") -> dict[str, Any]:
    return {
        "id": "inter-bind",
        "token": "token-bind",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": "bind",
                    "options": [{"type": 3, "name": "workspace", "value": path}],
                }
            ],
        },
    }


@pytest.mark.anyio
async def test_pma_on_enables_pma_mode(tmp_path: Path) -> None:
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
    gateway = _FakeGateway([_pma_interaction(subcommand="on")])
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
        assert "PMA mode enabled" in payload["data"]["content"]

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is True
        assert binding.get("pma_prev_workspace_path") == str(workspace)
        assert binding.get("pma_prev_repo_id") == "repo-1"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_off_disables_pma_mode_and_restores_binding(tmp_path: Path) -> None:
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
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id="repo-1",
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="off")])
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
        assert "PMA mode disabled" in payload["data"]["content"]
        assert "Restored" in payload["data"]["content"]

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is False
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_status_shows_disabled(tmp_path: Path) -> None:
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
    gateway = _FakeGateway([_pma_interaction(subcommand="status")])
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
        content = payload["data"]["content"]
        assert "PMA mode: disabled" in content
        assert str(workspace) in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_on_unbound_channel_auto_binds_for_pma(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="on")])
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
        assert "PMA mode enabled" in payload["data"]["content"]
        assert "Use /pma off to exit." in payload["data"]["content"]

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is True
        assert binding.get("pma_prev_workspace_path") is None
        assert binding.get("pma_prev_repo_id") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_off_after_unbound_pma_on_returns_to_unbound(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    rest = _FakeRest()
    gateway = _FakeGateway(
        [_pma_interaction(subcommand="on"), _pma_interaction(subcommand="off")]
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
        assert len(rest.interaction_responses) == 2
        on_payload = rest.interaction_responses[0]["payload"]["data"]["content"]
        off_payload = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert "PMA mode enabled" in on_payload
        assert "PMA mode disabled" in off_payload
        assert "Back to repo mode." in off_payload

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_status_unbound_channel_reports_disabled(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="status")])
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
        content = payload["data"]["content"]
        assert "PMA mode: disabled" in content
        assert "Current workspace: unbound" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_off_unbound_channel_is_idempotent(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="off")])
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
        assert "PMA mode disabled" in payload["data"]["content"]
        assert "Back to repo mode." in payload["data"]["content"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_disabled_in_config_returns_actionable_message(
    tmp_path: Path,
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
    gateway = _FakeGateway([_pma_interaction(subcommand="on")])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"}), pma_enabled=False),
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
        content = payload["data"]["content"]
        assert "disabled" in content.lower()
        assert "pma.enabled" in content.lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_command_registration_includes_pma_commands() -> None:
    from codex_autorunner.integrations.discord.commands import (
        build_application_commands,
    )

    commands = build_application_commands()
    command_names = {cmd["name"] for cmd in commands}
    assert "pma" in command_names

    pma_cmd = next(cmd for cmd in commands if cmd["name"] == "pma")
    subcommand_names = {opt["name"] for opt in pma_cmd.get("options", [])}
    assert "on" in subcommand_names
    assert "off" in subcommand_names
    assert "status" in subcommand_names


@pytest.mark.anyio
async def test_pma_off_only_clears_sink_when_matching_channel(tmp_path: Path) -> None:
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
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id="repo-1",
    )

    sink_store = PmaActiveSinkStore(tmp_path)
    sink_store.set_chat(platform="discord", chat_id="channel-2")

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="off")])
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
        assert "PMA mode disabled" in payload["data"]["content"]

        sink = sink_store.load()
        assert sink is not None
        assert sink.get("platform") == "discord"
        assert sink.get("chat_id") == "channel-2"

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is False
    finally:
        await store.close()
