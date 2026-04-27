from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.flows import FlowRunStatus
from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationPolicy,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.command_sync_calls: list[dict[str, Any]] = []
        self.fetched_channel_messages: dict[tuple[str, str], dict[str, Any]] = {}
        self._typing_event: asyncio.Event | None = None

    def _new_typing_event(self) -> asyncio.Event:
        self._typing_event = asyncio.Event()
        return self._typing_event

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
        message_id = f"msg-{len(self.channel_messages) + 1}"
        self.channel_messages.append(
            {
                "channel_id": channel_id,
                "payload": payload,
                "message_id": message_id,
            }
        )
        return {"id": message_id, "channel_id": channel_id, "payload": payload}

    async def get_channel_message(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        return dict(self.fetched_channel_messages.get((channel_id, message_id), {}))

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": payload,
            }
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)
        if self._typing_event is not None:
            self._typing_event.set()

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

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return {"id": "@original"}

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
    def __init__(
        self, events: list[dict[str, Any] | tuple[str, dict[str, Any]]]
    ) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for item in self._events:
            if isinstance(item, tuple):
                event_type, payload = item
            else:
                event_type, payload = "INTERACTION_CREATE", item
            await on_dispatch(event_type, payload)

    async def stop(self) -> None:
        self.stopped = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


def _latest_public_response_payload(rest: _FakeRest) -> dict[str, Any]:
    if rest.edited_original_interaction_responses:
        return rest.edited_original_interaction_responses[-1]["payload"]
    if rest.followup_messages:
        return rest.followup_messages[-1]["payload"]
    raise AssertionError("expected a Discord public response payload")


def _config(
    root: Path,
    *,
    allow_user_ids: frozenset[str],
    command_registration_enabled: bool = True,
    command_scope: str = "guild",
    command_guild_ids: tuple[str, ...] = ("guild-1",),
    collaboration_policy: CollaborationPolicy | None = None,
    ack_budget_ms: int = 10_000,
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
        dispatch=DiscordBotDispatchConfig(ack_budget_ms=ack_budget_ms),
        collaboration_policy=collaboration_policy,
    )


def _interaction(
    *,
    name: str,
    options: list[dict[str, Any]],
    user_id: str = "user-1",
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "token": interaction_token,
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": name, "options": options}],
        },
    }


def _interaction_path(
    *,
    command_path: tuple[str, ...],
    options: list[dict[str, Any]],
    user_id: str = "user-1",
) -> dict[str, Any]:
    assert command_path and command_path[0] == "car"
    if len(command_path) == 2:
        return _interaction(name=command_path[1], options=options, user_id=user_id)
    if len(command_path) == 3:
        return {
            "id": "inter-1",
            "token": "token-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "member": {"user": {"id": user_id}},
            "data": {
                "name": "car",
                "options": [
                    {
                        "type": 2,
                        "name": command_path[1],
                        "options": [
                            {
                                "type": 1,
                                "name": command_path[2],
                                "options": options,
                            }
                        ],
                    }
                ],
            },
        }
    raise AssertionError(f"Unsupported command path for test helper: {command_path}")


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


def _autocomplete_interaction_path(
    *,
    command_path: tuple[str, ...],
    focused_name: str,
    focused_value: str,
    user_id: str = "user-1",
) -> dict[str, Any]:
    assert command_path and command_path[0] == "car"
    if len(command_path) == 2:
        return _autocomplete_interaction(
            name=command_path[1],
            focused_name=focused_name,
            focused_value=focused_value,
            user_id=user_id,
        )
    if len(command_path) == 3:
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
                        "type": 2,
                        "name": command_path[1],
                        "options": [
                            {
                                "type": 1,
                                "name": command_path[2],
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
                    }
                ],
            },
        }
    raise AssertionError(f"Unsupported command path for test helper: {command_path}")


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


async def _dispatch_gateway_interaction(
    service: DiscordBotService,
    payload: dict[str, Any],
) -> None:
    await service._on_dispatch("INTERACTION_CREATE", payload)
    await asyncio.wait_for(service._command_runner.shutdown(), timeout=3.0)


@pytest.mark.anyio
async def test_service_flow_run_autocomplete_filters_for_action_and_query(
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
            _autocomplete_interaction_path(
                command_path=("car", "flow", "resume"),
                focused_name="run_id",
                focused_value="run-b",
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

    class _Run:
        def __init__(self, run_id: str, status: FlowRunStatus) -> None:
            self.id = run_id
            self.status = status

    class _Store:
        def list_flow_runs(self, *, flow_type: str) -> list[Any]:
            assert flow_type == "ticket_flow"
            return [
                _Run("run-a", FlowRunStatus.RUNNING),
                _Run("run-b", FlowRunStatus.PAUSED),
            ]

        def close(self) -> None:
            return None

    service._open_flow_store = lambda _workspace_root: _Store()  # type: ignore[assignment]

    try:
        await service.run_forever()
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        assert [entry["value"] for entry in payload["data"]["choices"]] == ["run-b"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_flow_resume_with_partial_run_id_prompts_filtered_picker(
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

    class _Run:
        def __init__(self, run_id: str, status: FlowRunStatus) -> None:
            self.id = run_id
            self.status = status

    class _Store:
        def list_flow_runs(self, *, flow_type: str) -> list[Any]:
            assert flow_type == "ticket_flow"
            return [
                _Run("run-alpha", FlowRunStatus.PAUSED),
                _Run("run-beta", FlowRunStatus.PAUSED),
            ]

        def close(self) -> None:
            return None

    service._open_flow_store = lambda _workspace_root: _Store()  # type: ignore[assignment]

    try:
        await _dispatch_gateway_interaction(
            service,
            _interaction_path(
                command_path=("car", "flow", "resume"),
                options=[{"name": "run_id", "value": "run"}],
            ),
        )
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        payload = rest.followup_messages[0]["payload"]
        content = payload["content"].lower()
        assert "matched 2 runs" in content
        select = payload["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["run-alpha", "run-beta"]
    finally:
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
async def test_car_flow_resume_status_text_prompts_picker_instead_of_auto_resolve(
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
            _interaction_path(
                command_path=("car", "flow", "resume"),
                options=[{"name": "run_id", "value": "paused"}],
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

    class _Run:
        def __init__(self, run_id: str, status: FlowRunStatus) -> None:
            self.id = run_id
            self.status = status

    class _Store:
        def list_flow_runs(self, *, flow_type: str) -> list[Any]:
            assert flow_type == "ticket_flow"
            return [
                _Run("run-paused-a", FlowRunStatus.PAUSED),
                _Run("run-paused-b", FlowRunStatus.PAUSED),
                _Run("run-running", FlowRunStatus.RUNNING),
            ]

        def close(self) -> None:
            return None

    service._open_flow_store = lambda _workspace_root: _Store()  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        payload = rest.followup_messages[0]["payload"]
        content = payload["content"].lower()
        assert "matched 2 runs" in content
        select = payload["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["run-paused-a", "run-paused-b"]
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
        service_or_self: Any,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
        deferred: bool = False,
    ) -> None:
        _ = interaction_id, interaction_token, workspace_root, deferred
        captured["action"] = action

    service._prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]
    from codex_autorunner.integrations.discord import flow_commands as _fc

    _fc.prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]

    try:
        await _dispatch_gateway_interaction(
            service,
            _interaction_path(command_path=("car", "flow", "restart"), options=[]),
        )
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
        service_or_self: Any,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
        deferred: bool = False,
    ) -> None:
        _ = interaction_id, interaction_token, workspace_root, action, deferred
        return

    service._prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]
    from codex_autorunner.integrations.discord import flow_commands as _fc

    _fc.prompt_flow_action_picker = _fake_prompt  # type: ignore[assignment]

    try:
        await _dispatch_gateway_interaction(
            service,
            _interaction_path(
                command_path=("car", "flow", "reply"),
                options=[{"type": 3, "name": "text", "value": "reply via picker"}],
            ),
        )
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
        component_response: bool = False,
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
        captured["component_response"] = component_response

    service._handle_flow_reply = _fake_handle_flow_reply  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert captured["options"]["run_id"] == "run-1"
        assert captured["options"]["text"] == "reply from pending"
        assert captured["component_response"] is True
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 6
        assert len(rest.edited_original_interaction_responses) == 1
        assert (
            rest.edited_original_interaction_responses[0]["payload"]["content"]
            == "Saving reply and resuming run run-1..."
        )
        assert (
            rest.edited_original_interaction_responses[0]["payload"]["components"] == []
        )
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
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "reply selection expired" in content
        assert (
            service._pending_flow_reply_text["channel-1:user-2"] == "reply from user2"
        )
    finally:
        await store.close()
