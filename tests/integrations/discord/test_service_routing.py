from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.integrations.app_server.client import CodexAppServerResponseError
from codex_autorunner.integrations.app_server.threads import (
    FILE_CHAT_PREFIX,
    PMA_OPENCODE_KEY,
)
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.errors import DiscordAPIError
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestRepo,
    save_manifest,
)


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
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
        self.channel_messages.append(
            {
                "channel_id": channel_id,
                "payload": payload,
            }
        )
        return {"id": "msg-1", "channel_id": channel_id, "payload": payload}

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": "followup-1"}

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


class _InitialResponseFailingRest(_FakeRest):
    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        raise DiscordAPIError("simulated initial response failure")


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


def _bind_select_interaction(
    *, selected_repo_id: str = "repo-1", user_id: str = "user-1"
) -> dict[str, Any]:
    return {
        "id": "inter-component-1",
        "token": "token-component-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 3,
        "member": {"user": {"id": user_id}},
        "data": {
            "component_type": 3,
            "custom_id": "bind_select",
            "values": [selected_repo_id],
        },
    }


def _autocomplete_interaction(
    *,
    name: str,
    focused_name: str,
    focused_value: str,
    user_id: str = "user-1",
) -> dict[str, Any]:
    return {
        "id": "inter-autocomplete-1",
        "token": "token-autocomplete-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 4,
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": name,
                    "options": [
                        {
                            "type": 3,
                            "name": focused_name,
                            "value": focused_value,
                            "focused": True,
                        }
                    ],
                }
            ],
        },
    }


def _component_interaction(
    *, custom_id: str | None, values: list[Any] | None = None, user_id: str = "user-1"
) -> dict[str, Any]:
    data: dict[str, Any] = {"component_type": 3}
    if custom_id is not None:
        data["custom_id"] = custom_id
    if values is not None:
        data["values"] = values
    return {
        "id": "inter-component-1",
        "token": "token-component-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 3,
        "member": {"user": {"id": user_id}},
        "data": data,
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


def _normalized_component_event(
    *, component_id: str, values: list[str] | None = None, user_id: str = "user-1"
) -> ChatInteractionEvent:
    thread = ChatThreadRef(platform="discord", chat_id="channel-1", thread_id="guild-1")
    return ChatInteractionEvent(
        update_id="discord:normalized:component:1",
        thread=thread,
        interaction=ChatInteractionRef(thread=thread, interaction_id="inter-1"),
        from_user_id=user_id,
        payload=json.dumps(
            {
                "_discord_interaction_id": "inter-1",
                "_discord_token": "token-1",
                "type": "component",
                "component_id": component_id,
                "values": values or [],
                "guild_id": "guild-1",
            },
            separators=(",", ":"),
        ),
    )


def test_model_picker_items_are_deduplicated_and_labeled() -> None:
    payload = {
        "models": [
            {"model": "gpt-5.3-codex"},
            {"id": "gpt-5.3-codex"},
            {"model": "openai/gpt-4o", "displayName": "GPT-4o"},
        ]
    }
    items = discord_service_module._coerce_model_picker_items(payload)
    assert items == [
        ("gpt-5.3-codex", "gpt-5.3-codex"),
        ("openai/gpt-4o", "openai/gpt-4o (GPT-4o)"),
    ]


@pytest.mark.anyio
async def test_model_list_with_agent_compat_retries_without_agent() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def model_list(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if "agent" in kwargs:
                raise CodexAppServerResponseError(
                    method="model/list",
                    code=-32602,
                    message="invalid params",
                )
            return {"data": [{"model": "gpt-5.3-codex"}]}

    client = _FakeClient()
    result = await discord_service_module._model_list_with_agent_compat(
        client,
        params={"agent": "codex", "limit": 25},
    )
    assert result == {"data": [{"model": "gpt-5.3-codex"}]}
    assert client.calls == [
        {"agent": "codex", "limit": 25},
        {"limit": 25},
    ]


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
async def test_service_bind_picker_prioritizes_recent_worktrees_when_truncated(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    repos = [
        ManifestRepo(
            id=f"base-{index:02d}",
            path=Path(f"repos/base-{index:02d}"),
            kind="base",
        )
        for index in range(26)
    ]
    repos.append(
        ManifestRepo(
            id="base-00--new-worktree",
            path=Path("worktrees/base-00--new-worktree"),
            kind="worktree",
            worktree_of="base-00",
            branch="new-worktree",
        )
    )
    save_manifest(
        manifest_path,
        Manifest(version=MANIFEST_VERSION, repos=repos),
        tmp_path,
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="bind", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        content = payload["data"]["content"]
        assert "page 1/2" in content
        menu = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in menu["options"]]
        assert len(values) == 25
        assert "base-00--new-worktree" in values
        assert "base-00" not in values
        nav = payload["data"]["components"][1]["components"]
        assert [button["label"] for button in nav] == ["Prev", "Page 1/2", "Next"]
        assert nav[0]["disabled"] is True
        assert nav[1]["disabled"] is True
        assert nav[2]["disabled"] is False
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_accepts_repo_id_as_workspace_option(tmp_path: Path) -> None:
    workspace = tmp_path / "worktrees" / "repo-1"
    workspace.mkdir(parents=True)
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    save_manifest(
        manifest_path,
        Manifest(
            version=MANIFEST_VERSION,
            repos=[
                ManifestRepo(
                    id="repo-1",
                    path=Path("worktrees/repo-1"),
                    kind="worktree",
                    worktree_of="base-1",
                    branch="feature/repo-1",
                )
            ],
        ),
        tmp_path,
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": "repo-1"}],
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
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] == "repo-1"
        assert binding["workspace_path"] == str(workspace.resolve())
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_bind_page_component_interaction(tmp_path: Path) -> None:
    repos = [(f"repo-{index:02d}", f"/tmp/repo-{index:02d}") for index in range(30)]

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_component_interaction(custom_id="bind_page:1")])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._list_manifest_repos = lambda: repos

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 7
        menu = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in menu["options"]]
        assert values == [f"repo-{index:02d}" for index in range(25, 30)]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_workspace_autocomplete_returns_matching_repo_ids(
    tmp_path: Path,
) -> None:
    repos = [
        ("codex-autorunner--discord-1", "/tmp/worktrees/codex-autorunner--discord-1"),
        ("codex-autorunner--discord-2", "/tmp/worktrees/codex-autorunner--discord-2"),
        ("ios-app-template", "/tmp/repos/ios-app-template"),
    ]
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _autocomplete_interaction(
                name="bind",
                focused_name="workspace",
                focused_value="discord",
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
    service._list_manifest_repos = lambda: repos

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        choices = payload["data"]["choices"]
        assert [choice["value"] for choice in choices] == [
            "codex-autorunner--discord-1",
            "codex-autorunner--discord-2",
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_workspace_autocomplete_keeps_repo_id_aliases_for_shared_path(
    tmp_path: Path,
) -> None:
    shared_workspace = tmp_path / "worktrees" / "shared"
    shared_workspace.mkdir(parents=True)
    repos = [
        ("repo-primary", str(shared_workspace)),
        ("repo-alias", str(shared_workspace)),
    ]
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _autocomplete_interaction(
                name="bind",
                focused_name="workspace",
                focused_value="repo-",
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
    service._list_manifest_repos = lambda: repos

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        choices = payload["data"]["choices"]
        values = [choice["value"] for choice in choices]
        assert "repo-primary" in values
        assert "repo-alias" in values
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_accepts_repo_alias_when_manifest_repos_share_workspace_path(
    tmp_path: Path,
) -> None:
    shared_workspace = tmp_path / "worktrees" / "shared"
    shared_workspace.mkdir(parents=True)
    repos = [
        ("repo-primary", str(shared_workspace)),
        ("repo-alias", str(shared_workspace)),
    ]
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": "repo-alias"}],
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
    service._list_manifest_repos = lambda: repos

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] == "repo-alias"
        assert binding["workspace_path"] == str(shared_workspace.resolve())
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_workspace_autocomplete_long_repo_id_uses_token(
    tmp_path: Path,
) -> None:
    long_repo_id = "repo-" + ("x" * 140)
    repos = [(long_repo_id, "/tmp/worktrees/repo-long")]
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _autocomplete_interaction(
                name="bind",
                focused_name="workspace",
                focused_value="repo-",
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
    service._list_manifest_repos = lambda: repos

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        choices = payload["data"]["choices"]
        assert len(choices) == 1
        assert choices[0]["value"].startswith("repo@")
        assert len(choices[0]["value"]) <= 100
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_accepts_autocomplete_repo_token(tmp_path: Path) -> None:
    long_repo_id = "repo-" + ("y" * 140)
    workspace = tmp_path / "worktrees" / "repo-token"
    workspace.mkdir(parents=True)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="bind", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._list_manifest_repos = lambda: [(long_repo_id, str(workspace))]
    token = service._repo_autocomplete_value(long_repo_id)
    assert token.startswith("repo@")

    bind_gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": token}],
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=bind_gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._list_manifest_repos = lambda: [(long_repo_id, str(workspace))]

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] == long_repo_id
        assert binding["workspace_path"] == str(workspace.resolve())
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_accepts_disabled_repo_id(tmp_path: Path) -> None:
    workspace = tmp_path / "worktrees" / "repo-disabled"
    workspace.mkdir(parents=True)
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    save_manifest(
        manifest_path,
        Manifest(
            version=MANIFEST_VERSION,
            repos=[
                ManifestRepo(
                    id="repo-disabled",
                    path=Path("worktrees/repo-disabled"),
                    kind="worktree",
                    worktree_of="base-1",
                    branch="feature/repo-disabled",
                    enabled=False,
                )
            ],
        ),
        tmp_path,
    )
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": "repo-disabled"}],
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
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] == "repo-disabled"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_bind_picker_component_interaction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_bind_select_interaction(selected_repo_id="repo-1")])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._list_manifest_repos = lambda: [("repo-1", str(workspace))]

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] == "repo-1"
        assert binding["workspace_path"] == str(workspace.resolve())

        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "bound this channel to: repo-1" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_missing_custom_id_returns_error(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_component_interaction(custom_id=None, values=["repo-1"])])
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
        assert "could not identify this interaction action" in content
        assert await store.get_binding(channel_id="channel-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("custom_id", "expected_error_snippet"),
    [
        ("bind_select", "please select a repository"),
        ("flow_runs_select", "please select a run"),
        ("agent_select", "please select an agent"),
        ("model_select", "please select a model"),
        ("model_effort_select", "please select reasoning effort"),
        ("session_resume_select", "please select a thread"),
        ("update_target_select", "please select an update target"),
        ("review_commit_select", "please select a commit"),
        ("flow_action_select:status", "please select a run"),
    ],
)
async def test_component_interaction_with_empty_values_returns_error(
    tmp_path: Path, custom_id: str, expected_error_snippet: str
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_component_interaction(custom_id=custom_id, values=[])])
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
        assert expected_error_snippet in content
        assert await store.get_binding(channel_id="channel-1") is None
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_malformed_direct_payload_returns_parse_error(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            {
                "id": "inter-1",
                "token": "token-1",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "member": {"user": {"id": "user-1"}},
                "data": "malformed",
            }
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "could not parse this interaction" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_direct_payload_missing_token_remains_unanswered(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            {
                "id": "inter-1",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "member": {"user": {"id": "user-1"}},
                "data": {"name": "car"},
            }
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
        assert rest.interaction_responses == []
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
async def test_service_falls_back_to_followup_when_initial_response_fails(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _InitialResponseFailingRest()
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
        assert rest.interaction_responses == []
        assert len(rest.followup_messages) == 1
        payload = rest.followup_messages[0]["payload"]
        assert payload["flags"] == 64
        assert "not bound" in payload["content"].lower()
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
async def test_car_agent_without_name_returns_picker_when_bound(tmp_path: Path) -> None:
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
    gateway = _FakeGateway([_interaction(name="agent", options=[])])
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
        data = rest.interaction_responses[0]["payload"]["data"]
        assert "select an agent" in data["content"].lower()
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "agent_select"
        values = {opt["value"] for opt in menu["options"]}
        assert values == {"codex", "opencode"}
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_agent_select_updates_agent(tmp_path: Path) -> None:
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
    gateway = _FakeGateway(
        [_component_interaction(custom_id="agent_select", values=["opencode"])]
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
        assert binding.get("agent") == "opencode"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "agent set to opencode" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_model_without_name_returns_picker_when_bound(tmp_path: Path) -> None:
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
    gateway = _FakeGateway([_interaction(name="model", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _FakeModelClient:
        async def model_list(self, **kwargs: Any) -> Any:
            _ = kwargs
            return {
                "data": [
                    {"model": "gpt-5.3-codex"},
                    {"model": "openai/gpt-4o", "displayName": "GPT-4o"},
                ]
            }

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _FakeModelClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        data = rest.followup_messages[0]["payload"]
        assert "select a model override" in data["content"].lower()
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "model_select"
        values = [opt["value"] for opt in menu["options"]]
        assert "clear" in values
        assert "gpt-5.3-codex" in values
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_model_without_name_defers_before_model_lookup(
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
    gateway = _FakeGateway([_interaction(name="model", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        return None

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "workspace unavailable for model picker" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_model_without_name_falls_back_when_model_list_fails(
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
    gateway = _FakeGateway([_interaction(name="model", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _FailingModelClient:
        async def model_list(self, **kwargs: Any) -> Any:
            _ = kwargs
            raise RuntimeError("boom")

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _FailingModelClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        data = rest.followup_messages[0]["payload"]
        content = data["content"].lower()
        assert "failed to list models for picker" in content
        assert "use `/car model name:<id>` to set a model" in content
        assert "components" not in data
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_model_select_updates_model(tmp_path: Path) -> None:
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
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_component_interaction(custom_id="model_select", values=["gpt-5.3-codex"])]
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
        assert binding.get("model_override") == "gpt-5.3-codex"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "model set to gpt-5.3-codex" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_model_select_prompts_effort_for_codex(
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
    gateway = _FakeGateway(
        [_component_interaction(custom_id="model_select", values=["gpt-5.3-codex"])]
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
        assert binding.get("model_override") is None
        assert len(rest.interaction_responses) == 1
        data = rest.interaction_responses[0]["payload"]["data"]
        assert "select reasoning effort" in data["content"].lower()
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "model_effort_select"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_model_effort_select_updates_model(
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
    gateway = _FakeGateway(
        [_component_interaction(custom_id="model_effort_select", values=["high"])]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._pending_model_effort["channel-1:user-1"] = "gpt-5.3-codex"
    service._pending_model_effort["channel-1:user-2"] = "openai/gpt-4o"

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("model_override") == "gpt-5.3-codex"
        assert binding.get("reasoning_effort") == "high"
        assert service._pending_model_effort["channel-1:user-2"] == "openai/gpt-4o"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_model_effort_pending_state_is_user_scoped(
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
    gateway = _FakeGateway(
        [
            _component_interaction(
                custom_id="model_effort_select",
                values=["high"],
                user_id="user-1",
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1", "user-2"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._pending_model_effort["channel-1:user-2"] = "gpt-5.3-codex"

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("model_override") is None
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "model selection expired" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_car_new_without_generic_fallback(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="new", options=[])])
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
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
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
async def test_normalized_component_agent_select_updates_agent(tmp_path: Path) -> None:
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
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        event = _normalized_component_event(
            component_id="agent_select",
            values=["opencode"],
        )
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("agent") == "opencode"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "agent set to opencode" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_normalized_component_model_select_updates_model(tmp_path: Path) -> None:
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
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
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
        event = _normalized_component_event(
            component_id="model_select",
            values=["gpt-5.3-codex"],
        )
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("model_override") == "gpt-5.3-codex"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "model set to gpt-5.3-codex" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_normalized_interaction_session_resume_without_thread_uses_picker(
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
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _FakeOrchestrator:
        def get_thread_id(self, _session_key: str) -> str | None:
            return "thread-1"

        def set_thread_id(self, _session_key: str, _thread_id: str) -> None:
            return None

    fake_orchestrator = _FakeOrchestrator()

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return fake_orchestrator

    async def _fake_list_threads(*args: Any, **kwargs: Any) -> list[tuple[str, str]]:
        _ = args, kwargs
        return [("thread-1", "thread-1 (current)"), ("thread-2", "thread-2")]

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]
    service._list_session_threads_for_picker = _fake_list_threads  # type: ignore[assignment]

    try:
        event = _normalized_interaction_event(command="car:session:resume")
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 2
        picker_payload = rest.followup_messages[1]["payload"]
        components = picker_payload.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "session_resume_select"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_session_resume_select_routes_to_resume(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_component_interaction(custom_id="session_resume_select", values=["th-2"])]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, Any] = {}

    async def _fake_handle_car_resume(
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        _ = interaction_id, interaction_token
        captured["channel_id"] = channel_id
        captured["options"] = options

    service._handle_car_resume = _fake_handle_car_resume  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert captured["channel_id"] == "channel-1"
        assert captured["options"]["thread_id"] == "th-2"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_normalized_interaction_flow_restart_without_run_id_uses_picker(
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
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, Any] = {}

    async def _fake_prompt(
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
    ) -> None:
        _ = interaction_id, interaction_token, workspace_root
        captured["action"] = action

    service._prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]

    try:
        event = _normalized_interaction_event(command="car:flow:restart")
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        assert captured["action"] == "restart"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_normalized_interaction_flow_reply_without_run_id_sets_pending_text(
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
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_prompt(
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
    ) -> None:
        _ = interaction_id, interaction_token, workspace_root, action
        return

    service._prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]

    try:
        event = _normalized_interaction_event(
            command="car:flow:reply",
            options={"text": "reply via picker"},
        )
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        assert (
            service._pending_flow_reply_text["channel-1:user-1"] == "reply via picker"
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_flow_action_reply_uses_pending_text(
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
    gateway = _FakeGateway(
        [_component_interaction(custom_id="flow_action_select:reply", values=["run-1"])]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._pending_flow_reply_text["channel-1:user-1"] = "reply from pending"
    service._pending_flow_reply_text["channel-1:user-2"] = "other pending"
    captured: dict[str, Any] = {}

    async def _fake_handle_flow_reply(
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: str | None = None,
        guild_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        _ = (
            interaction_id,
            interaction_token,
            workspace_root,
            channel_id,
            guild_id,
            user_id,
        )
        captured["options"] = options

    service._handle_flow_reply = _fake_handle_flow_reply  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert captured["options"]["run_id"] == "run-1"
        assert captured["options"]["text"] == "reply from pending"
        assert service._pending_flow_reply_text["channel-1:user-2"] == "other pending"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_flow_reply_pending_state_is_user_scoped(
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
    gateway = _FakeGateway(
        [
            _component_interaction(
                custom_id="flow_action_select:reply",
                values=["run-1"],
                user_id="user-1",
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1", "user-2"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._pending_flow_reply_text["channel-1:user-2"] = "reply from user2"

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "reply selection expired" in content
        assert (
            service._pending_flow_reply_text["channel-1:user-2"] == "reply from user2"
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_review_commit_without_sha_returns_picker(tmp_path: Path) -> None:
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
    gateway = _FakeGateway(
        [
            _interaction(
                name="review",
                options=[{"type": 3, "name": "target", "value": "commit"}],
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

    async def _fake_list_recent_commits(
        *_args: Any, **_kwargs: Any
    ) -> list[tuple[str, str]]:
        return [("abcdef1234567890", "Fix picker")]

    service._list_recent_commits_for_picker = _fake_list_recent_commits  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        data = rest.interaction_responses[0]["payload"]["data"]
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "review_commit_select"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_review_commit_select_routes_to_review(
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
    gateway = _FakeGateway(
        [_component_interaction(custom_id="review_commit_select", values=["abcdef1"])]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, Any] = {}

    async def _fake_handle_review(
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        _ = interaction_id, interaction_token, channel_id, workspace_root
        captured["target"] = options.get("target")

    service._handle_car_review = _fake_handle_review  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert captured["target"] == "commit abcdef1"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_review_custom_without_instructions_returns_guidance(
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
    gateway = _FakeGateway(
        [
            _interaction(
                name="review",
                options=[{"type": 3, "name": "target", "value": "custom"}],
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
    deferred = False

    async def _fake_defer_ephemeral(*_args: Any, **_kwargs: Any) -> None:
        nonlocal deferred
        deferred = True

    service._defer_ephemeral = _fake_defer_ephemeral  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert deferred is False
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "provide custom review instructions" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_review_target_commitment_is_treated_as_custom(
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
    gateway = _FakeGateway(
        [
            _interaction(
                name="review",
                options=[{"type": 3, "name": "target", "value": "commitment"}],
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

    async def _fake_run_agent_turn_for_message(**kwargs: Any) -> str:
        return str(kwargs.get("prompt_text", ""))

    service._run_agent_turn_for_message = _fake_run_agent_turn_for_message  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.channel_messages) == 1
        content = rest.channel_messages[0]["payload"]["content"]
        assert "Review instructions: commitment" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_without_target_returns_picker(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="update", options=[])])
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
        data = rest.interaction_responses[0]["payload"]["data"]
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "update_target_select"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_update_target_select_routes_update(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_component_interaction(custom_id="update_target_select", values=["discord"])]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, Any] = {}

    async def _fake_handle_update(
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        _ = interaction_id, interaction_token, channel_id
        captured["target"] = options.get("target")

    service._handle_car_update = _fake_handle_update  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert captured["target"] == "discord"
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("component_id", "expected"),
    [
        ("agent_select", "please select an agent"),
        ("model_select", "please select a model"),
    ],
)
async def test_normalized_component_empty_values_returns_error(
    tmp_path: Path,
    component_id: str,
    expected: str,
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
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        event = _normalized_component_event(component_id=component_id, values=[])
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert expected in content
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


@pytest.mark.anyio
async def test_malformed_interaction_payload_returns_ephemeral_response(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            {
                "id": "inter-1",
                "token": "token-1",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "member": {"user": {"id": "user-1"}},
                "data": {"name": ""},
            }
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
        content = payload["data"]["content"].lower()
        assert "could not parse this interaction" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_new_resets_repo_session_key(tmp_path: Path) -> None:
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
    gateway = _FakeGateway([_interaction(name="new", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _FakeOrchestrator:
        def __init__(self) -> None:
            self.reset_keys: list[str] = []

        def reset_thread_id(self, session_key: str) -> bool:
            self.reset_keys.append(session_key)
            return True

    fake_orchestrator = _FakeOrchestrator()

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return fake_orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert fake_orchestrator.reset_keys
        assert fake_orchestrator.reset_keys[0].startswith(FILE_CHAT_PREFIX)
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "fresh repo session" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_newt_resets_current_workspace_branch_and_session(
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
    gateway = _FakeGateway([_interaction(name="newt", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    branch_calls: list[dict[str, Any]] = []

    def _fake_reset_branch(repo_root: Path, branch_name: str) -> str:
        branch_calls.append({"repo_root": repo_root, "branch_name": branch_name})
        return "master"

    monkeypatch.setattr(
        discord_service_module, "reset_branch_from_origin_main", _fake_reset_branch
    )

    class _FakeOrchestrator:
        def __init__(self) -> None:
            self.reset_keys: list[str] = []

        def reset_thread_id(self, session_key: str) -> bool:
            self.reset_keys.append(session_key)
            return True

    fake_orchestrator = _FakeOrchestrator()

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return fake_orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        expected_branch = (
            "thread-channel-1-"
            f"{hashlib.sha256(str(workspace.resolve()).encode('utf-8')).hexdigest()[:10]}"
        )
        assert branch_calls == [
            {
                "repo_root": workspace.resolve(),
                "branch_name": expected_branch,
            }
        ]
        assert fake_orchestrator.reset_keys
        assert fake_orchestrator.reset_keys[0].startswith(FILE_CHAT_PREFIX)
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "reset branch" in content
        assert "origin/master" in content
        assert "fresh repo session" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_newt_runs_hub_setup_commands_for_bound_workspace(
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
    gateway = _FakeGateway([_interaction(name="newt", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    def _fake_reset_branch(_repo_root: Path, _branch_name: str) -> str:
        return "master"

    monkeypatch.setattr(
        discord_service_module, "reset_branch_from_origin_main", _fake_reset_branch
    )

    class _HubSupervisorStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_setup_commands_for_workspace(
            self, workspace_path: Path, *, repo_id_hint: str | None = None
        ) -> int:
            self.calls.append(
                {"workspace_path": workspace_path, "repo_id_hint": repo_id_hint}
            )
            return 1

    hub_supervisor = _HubSupervisorStub()
    service._hub_supervisor = hub_supervisor  # type: ignore[assignment]

    class _FakeOrchestrator:
        def reset_thread_id(self, _session_key: str) -> bool:
            return True

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return _FakeOrchestrator()

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert hub_supervisor.calls == [
            {"workspace_path": workspace.resolve(), "repo_id_hint": "repo-1"}
        ]
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "ran 1 setup command" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_newt_reports_branch_reset_errors(
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
    gateway = _FakeGateway([_interaction(name="newt", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    def _fail_reset_branch(_repo_root: Path, _branch_name: str) -> None:
        raise discord_service_module.GitError("simulated failure")

    monkeypatch.setattr(
        discord_service_module, "reset_branch_from_origin_main", _fail_reset_branch
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "failed to reset branch" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_new_resets_pma_session_key_for_current_agent(tmp_path: Path) -> None:
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
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id="repo-1",
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="new", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    class _FakeOrchestrator:
        def __init__(self) -> None:
            self.reset_keys: list[str] = []

        def reset_thread_id(self, session_key: str) -> bool:
            self.reset_keys.append(session_key)
            return True

    fake_orchestrator = _FakeOrchestrator()

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return fake_orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert fake_orchestrator.reset_keys == [PMA_OPENCODE_KEY]
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "fresh pma session" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_status_reports_absent_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discord_service_module, "_read_update_status", lambda: None)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="update",
                options=[{"type": 3, "name": "target", "value": "status"}],
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "no update status recorded" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_starts_worker_with_explicit_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="update",
                options=[{"type": 3, "name": "target", "value": "both"}],
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

    observed: dict[str, Any] = {}

    def _fake_spawn_update_process(**kwargs: Any) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(
        discord_service_module,
        "_spawn_update_process",
        _fake_spawn_update_process,
    )

    try:
        await service.run_forever()
        assert observed["update_target"] == "both"
        assert observed["repo_ref"] == "main"
        assert "codex-autorunner.git" in observed["repo_url"]
        assert observed["notify_platform"] == "discord"
        assert observed["notify_context"] == {"chat_id": "channel-1"}
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "update started (both)" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_run_forever_sends_pending_update_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discord_service_module,
        "_read_update_status",
        lambda: {
            "status": "ok",
            "message": "Update completed successfully.",
            "notify_platform": "discord",
            "notify_context": {"chat_id": "channel-1"},
            "notify_sent_at": None,
        },
    )
    marked: list[dict[str, Any]] = []

    def _fake_mark_update_status_notified(**kwargs: Any) -> None:
        marked.append(kwargs)

    monkeypatch.setattr(
        discord_service_module,
        "mark_update_status_notified",
        _fake_mark_update_status_notified,
    )
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
        assert len(rest.channel_messages) == 1
        content = rest.channel_messages[0]["payload"]["content"].lower()
        assert "update status: ok" in content
        assert marked
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_rejects_invalid_target(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="update",
                options=[{"type": 3, "name": "target", "value": "invalid-target"}],
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
        data = rest.interaction_responses[0]["payload"]["data"]
        content = data["content"].lower()
        assert "unsupported update target" in content
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "update_target_select"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_experimental_enable_without_feature_returns_usage(
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
    gateway = _FakeGateway(
        [
            _interaction(
                name="experimental",
                options=[{"type": 3, "name": "action", "value": "enable"}],
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "missing feature for `enable`" in content
        assert "/car experimental action:list" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_experimental_unknown_action_returns_guidance(
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
    gateway = _FakeGateway(
        [
            _interaction(
                name="experimental",
                options=[{"type": 3, "name": "action", "value": "toggle"}],
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
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "unknown action: toggle" in content
        assert "valid actions: list, enable, disable" in content
    finally:
        await store.close()


def _make_failing_orchestrator(workspace_root: Path) -> Any:
    class FailingOrchestrator:
        async def run_turn(
            self,
            agent: str,
            messages: list[dict[str, Any]],
            *,
            model_override: str | None = None,
            session_key: str,
            session_id: str | None = None,
            workspace_root: Path,
            reasoning_effort: str | None = None,
            autorunner_effort_override: str | None = None,
        ) -> Any:
            raise RuntimeError("Simulated backend error")

        def get_thread_id(self, session_key: str) -> str | None:
            return None

        def set_thread_id(self, session_key: str, thread_id: str) -> None:
            pass

        def close(self) -> None:
            pass

    return FailingOrchestrator()


@pytest.mark.anyio
async def test_car_command_handles_turn_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    await store.upsert_binding(
        channel_id="channel-1",
        guild_id=None,
        workspace_path=str(workspace),
        repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="new", options=[])])

    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        backend_orchestrator_factory=_make_failing_orchestrator,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) >= 1
        last_response = rest.interaction_responses[-1]
        content = last_response["payload"]["data"]["content"].lower()
        assert "error" in content or "failed" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_command_raises_on_invalid_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[
                    {"type": 3, "name": "workspace", "value": "/nonexistent/path"}
                ],
            )
        ]
    )

    class RaiseErrorBackendOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_turn(
            self,
            agent: str,
            messages: list[dict[str, Any]],
            *,
            model_override: str | None = None,
            session_key: str,
            session_id: str | None = None,
            workspace_root: Path,
            reasoning_effort: str | None = None,
            autorunner_effort_override: str | None = None,
        ) -> Any:
            raise RuntimeError("Simulated backend error")

        def get_thread_id(self, session_key: str) -> str | None:
            return None

        def set_thread_id(self, session_key: str, thread_id: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        discord_service_module,
        "BackendOrchestrator",
        RaiseErrorBackendOrchestrator,
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
        assert len(rest.interaction_responses) >= 1
        last_response = rest.interaction_responses[-1]
        content = last_response["payload"]["data"]["content"].lower()
        assert "workspace path does not exist" in content
    finally:
        await store.close()
