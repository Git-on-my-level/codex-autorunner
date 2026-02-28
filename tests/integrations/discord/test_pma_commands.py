from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.pma_delivery_targets import (
    PmaDeliveryTargetsStore,
    target_key,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
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
        message_overflow="split",
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


def _pma_root_interaction(*, user_id: str = "user-1") -> dict[str, Any]:
    return {
        "id": "inter-root",
        "token": "token-root",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "pma",
            "options": [],
        },
    }


def _pma_target_interaction(
    *, action: str, ref: str | None = None, user_id: str = "user-1"
) -> dict[str, Any]:
    subcommand: dict[str, Any] = {
        "type": 1,
        "name": action,
        "options": [],
    }
    if ref is not None:
        subcommand["options"] = [{"type": 3, "name": "ref", "value": ref}]
    return {
        "id": "inter-target",
        "token": "token-target",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "pma",
            "options": [{"type": 2, "name": "target", "options": [subcommand]}],
        },
    }


def _pma_thread_interaction(
    *,
    action: str,
    managed_thread_id: str | None = None,
    backend_id: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    repo: str | None = None,
    limit: int | None = None,
    user_id: str = "user-1",
) -> dict[str, Any]:
    subcommand_options: list[dict[str, Any]] = []
    if managed_thread_id is not None:
        subcommand_options.append({"type": 3, "name": "id", "value": managed_thread_id})
    if backend_id is not None:
        subcommand_options.append(
            {"type": 3, "name": "backend_id", "value": backend_id}
        )
    if agent is not None:
        subcommand_options.append({"type": 3, "name": "agent", "value": agent})
    if status is not None:
        subcommand_options.append({"type": 3, "name": "status", "value": status})
    if repo is not None:
        subcommand_options.append({"type": 3, "name": "repo", "value": repo})
    if limit is not None:
        subcommand_options.append({"type": 4, "name": "limit", "value": limit})
    subcommand: dict[str, Any] = {
        "type": 1,
        "name": action,
        "options": subcommand_options,
    }
    return {
        "id": "inter-thread",
        "token": "token-thread",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "pma",
            "options": [{"type": 2, "name": "thread", "options": [subcommand]}],
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

        targets_state = PmaDeliveryTargetsStore(tmp_path).load()
        keys = {
            key
            for key in (target_key(target) for target in targets_state["targets"])
            if isinstance(key, str)
        }
        assert keys == {"chat:discord:channel-1"}
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
async def test_pma_no_subcommand_defaults_to_status(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(tmp_path),
        repo_id="repo-1",
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=None,
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_pma_root_interaction()])
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "PMA mode: enabled" in content
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is True
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
async def test_pma_thread_list_info_archive_resume(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    thread_store = PmaThreadStore(tmp_path)
    thread = thread_store.create_thread(
        "codex",
        workspace,
        repo_id="repo-1",
        name="Integration test thread",
    )
    managed_thread_id = str(thread["managed_thread_id"])

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _pma_thread_interaction(action="list"),
            _pma_thread_interaction(action="info", managed_thread_id=managed_thread_id),
            _pma_thread_interaction(
                action="archive", managed_thread_id=managed_thread_id
            ),
            _pma_thread_interaction(
                action="resume",
                managed_thread_id=managed_thread_id,
                backend_id="backend-42",
            ),
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
        assert len(rest.interaction_responses) == 4
        assert (
            "Managed PMA threads:"
            in rest.interaction_responses[0]["payload"]["data"]["content"]
        )
        assert (
            managed_thread_id
            in rest.interaction_responses[0]["payload"]["data"]["content"]
        )
        assert (
            f"Managed thread: {managed_thread_id}"
            in rest.interaction_responses[1]["payload"]["data"]["content"]
        )
        assert (
            f"Archived managed thread: {managed_thread_id}"
            in rest.interaction_responses[2]["payload"]["data"]["content"]
        )
        assert (
            f"Resumed managed thread: {managed_thread_id} (backend=backend-42)"
            in rest.interaction_responses[3]["payload"]["data"]["content"]
        )

        updated = thread_store.get_thread(managed_thread_id)
        assert updated is not None
        assert updated.get("status") == "active"
        assert updated.get("backend_thread_id") == "backend-42"
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
    assert "targets" in subcommand_names
    assert "target" in subcommand_names
    assert "thread" in subcommand_names

    target_group = next(
        opt for opt in pma_cmd.get("options", []) if opt.get("name") == "target"
    )
    target_subcommands = {opt["name"] for opt in target_group.get("options", [])}
    assert target_subcommands == {"add", "rm", "clear"}

    thread_group = next(
        opt for opt in pma_cmd.get("options", []) if opt.get("name") == "thread"
    )
    thread_subcommands = {opt["name"] for opt in thread_group.get("options", [])}
    assert thread_subcommands == {"list", "info", "archive", "resume"}


@pytest.mark.anyio
async def test_pma_off_keeps_delivery_targets(tmp_path: Path) -> None:
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

    targets_store = PmaDeliveryTargetsStore(tmp_path)
    targets_store.set_targets(
        [
            {"kind": "chat", "platform": "discord", "chat_id": "channel-2"},
            {"kind": "chat", "platform": "telegram", "chat_id": "-1001"},
        ]
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

        keys = {
            key
            for key in (
                target_key(target) for target in targets_store.load().get("targets", [])
            )
            if isinstance(key, str)
        }
        assert keys == {"chat:discord:channel-2", "chat:telegram:-1001"}

        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("pma_enabled") is False
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_unknown_subcommand_returns_updated_usage(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_pma_interaction(subcommand="unknown")])
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Unknown PMA subcommand." in content
        assert "/pma on|off|status|targets" in content
        assert "/pma target add <ref>" in content
        assert "/pma target rm <ref>" in content
        assert "/pma thread list" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_target_remove_alias_is_not_supported(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _pma_target_interaction(action="add", ref="here"),
            _pma_target_interaction(action="remove", ref="here"),
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
        assert len(rest.interaction_responses) == 2
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert "Usage:" in content
        assert "/pma target rm <ref>" in content
        keys = {
            key
            for key in (
                target_key(target)
                for target in PmaDeliveryTargetsStore(tmp_path)
                .load()
                .get("targets", [])
            )
            if isinstance(key, str)
        }
        assert "chat:discord:channel-1" in keys
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_target_add_list_remove_and_clear(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _pma_target_interaction(action="add", ref="here"),
            _pma_target_interaction(action="add", ref="telegram:-2002:77"),
            _pma_target_interaction(action="add", ref="discord:99887766"),
            _pma_target_interaction(action="add", ref="chat:telegram:-3003:88"),
            _pma_target_interaction(action="add", ref="chat:discord:66554433"),
            _pma_target_interaction(action="add", ref="web"),
            _pma_target_interaction(action="add", ref="local:./notes/pma.md"),
            _pma_interaction(subcommand="targets"),
            _pma_target_interaction(action="rm", ref="here"),
            _pma_target_interaction(action="clear"),
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
        assert len(rest.interaction_responses) == 10
        list_content = rest.interaction_responses[7]["payload"]["data"]["content"]
        assert "chat:discord:channel-1" in list_content
        assert "chat:telegram:-2002:77" in list_content
        assert "chat:discord:99887766" in list_content
        assert "chat:telegram:-3003:88" in list_content
        assert "chat:discord:66554433" in list_content
        assert "web" in list_content
        assert "local:./notes/pma.md" in list_content

        assert "Removed PMA delivery target: chat:discord:channel-1" in (
            rest.interaction_responses[8]["payload"]["data"]["content"]
        )
        assert "Cleared PMA delivery targets." in (
            rest.interaction_responses[9]["payload"]["data"]["content"]
        )
        assert PmaDeliveryTargetsStore(tmp_path).load()["targets"] == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_pma_target_add_invalid_ref_returns_usage(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_pma_target_interaction(action="add", ref="telegram:abc")])
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Invalid target ref" in content
        assert "/pma target add <ref>" in content
        assert "Refs:" in content
        assert "web" in content
        assert "local:<path>" in content
        assert "chat:telegram:<chat_id>[:<thread_id>]" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_discord_can_add_telegram_target_to_delivery_store(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_pma_target_interaction(action="add", ref="telegram:-123:77")]
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
        keys = {
            key
            for key in (
                target_key(target)
                for target in PmaDeliveryTargetsStore(tmp_path)
                .load()
                .get("targets", [])
            )
            if isinstance(key, str)
        }
        assert "chat:telegram:-123:77" in keys
    finally:
        await store.close()
