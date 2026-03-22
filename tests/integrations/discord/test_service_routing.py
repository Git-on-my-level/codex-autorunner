from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.core.flows import FlowRunStatus
from codex_autorunner.integrations.app_server.client import CodexAppServerResponseError
from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationPolicy,
    build_discord_collaboration_policy,
)
from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
from codex_autorunner.integrations.chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatReplyInfo,
    ChatThreadRef,
)
from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.car_autocomplete import (
    repo_autocomplete_value,
    workspace_autocomplete_value,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.errors import DiscordAPIError
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
    DiscordMessageTurnResult,
)
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
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.command_sync_calls: list[dict[str, Any]] = []
        self.fetched_channel_messages: dict[tuple[str, str], dict[str, Any]] = {}

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


class _DeleteFailingRest(_FakeRest):
    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        _ = channel_id, message_id
        raise RuntimeError("delete failed")


@pytest.mark.anyio
async def test_discord_message_turns_route_through_orchestration_ingress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class _StoreStub:
        async def get_binding(self, *, channel_id: str) -> dict[str, object] | None:
            assert channel_id == "channel-1"
            return {
                "workspace_path": str(workspace),
                "agent": "codex",
                "pma_enabled": False,
                "model_override": None,
                "reasoning_effort": None,
            }

    class _ServiceStub:
        def __init__(self) -> None:
            self._store = _StoreStub()
            self._logger = logging.getLogger("test")

        def _normalize_agent(self, value: object) -> str:
            return str(value or "codex")

        def _build_message_session_key(self, **_kwargs: object) -> str:
            return "session-key"

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            captured["request"] = request
            captured["callbacks"] = set(kwargs)
            return SimpleNamespace(route="flow", thread_result=None)

    monkeypatch.setattr(
        discord_message_turns,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    event = ChatMessageEvent(
        update_id="update-1",
        thread=ChatThreadRef(platform="discord", chat_id="channel-1", thread_id=None),
        message=ChatMessageRef(
            thread=ChatThreadRef(
                platform="discord",
                chat_id="channel-1",
                thread_id=None,
            ),
            message_id="msg-1",
        ),
        from_user_id="user-1",
        text="hello",
    )
    context = build_dispatch_context(event)

    await discord_message_turns.handle_message_event(
        _ServiceStub(),
        event,
        context,
        channel_id="channel-1",
        text="hello",
        has_attachments=False,
        policy_result=None,
        log_event_fn=lambda *args, **kwargs: None,
        build_ticket_flow_controller_fn=lambda *_args, **_kwargs: None,
        ensure_worker_fn=lambda *_args, **_kwargs: None,
    )

    request = captured.get("request")
    assert request is not None
    assert request.surface_kind == "discord"
    assert request.prompt_text == "hello"
    assert request.workspace_root == workspace
    assert captured["callbacks"] == {
        "resolve_paused_flow_target",
        "submit_flow_reply",
        "submit_thread_message",
    }


@pytest.mark.anyio
async def test_discord_message_turns_include_reply_context_in_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class _StoreStub:
        async def get_binding(self, *, channel_id: str) -> dict[str, object] | None:
            assert channel_id == "channel-1"
            return {
                "workspace_path": str(workspace),
                "agent": "codex",
                "pma_enabled": False,
                "model_override": None,
                "reasoning_effort": None,
            }

    class _ServiceStub:
        def __init__(self) -> None:
            self._store = _StoreStub()
            self._logger = logging.getLogger("test")

        def _normalize_agent(self, value: object) -> str:
            return str(value or "codex")

        def _build_message_session_key(self, **_kwargs: object) -> str:
            return "session-key"

    class _IngressStub:
        async def submit_message(self, request, **kwargs):  # type: ignore[no-untyped-def]
            captured["request"] = request
            _ = kwargs
            return SimpleNamespace(route="flow", thread_result=None)

    monkeypatch.setattr(
        discord_message_turns,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    thread = ChatThreadRef(platform="discord", chat_id="channel-1", thread_id=None)
    reply_message = ChatMessageRef(thread=thread, message_id="msg-0")
    event = ChatMessageEvent(
        update_id="update-2",
        thread=thread,
        message=ChatMessageRef(thread=thread, message_id="msg-2"),
        from_user_id="user-1",
        text="hello",
        reply_to=reply_message,
        reply_context=ChatReplyInfo(
            message=reply_message,
            text="prior bot output",
            author_label="Codex",
            is_bot=True,
        ),
    )
    context = build_dispatch_context(event)

    await discord_message_turns.handle_message_event(
        _ServiceStub(),
        event,
        context,
        channel_id="channel-1",
        text="hello",
        has_attachments=False,
        policy_result=None,
        log_event_fn=lambda *args, **kwargs: None,
        build_ticket_flow_controller_fn=lambda *_args, **_kwargs: None,
        ensure_worker_fn=lambda *_args, **_kwargs: None,
    )

    request = captured.get("request")
    assert request is not None
    assert "hello" in request.prompt_text
    assert "Replying to message from Codex [message msg-0]:" in request.prompt_text
    assert "prior bot output" in request.prompt_text


@pytest.mark.anyio
async def test_discord_service_fetches_missing_reply_context_from_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    rest.fetched_channel_messages[("channel-1", "reply-1")] = {
        "id": "reply-1",
        "content": "fetched bot reply",
        "author": {
            "id": "bot-1",
            "bot": True,
            "global_name": "Codex",
            "username": "codexautorunner",
        },
    }
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    captured: dict[str, Any] = {}

    async def _fake_handle_message_event(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        captured["event"] = args[1]
        _ = kwargs

    monkeypatch.setattr(
        discord_service_module,
        "handle_discord_message_event",
        _fake_handle_message_event,
    )
    monkeypatch.setattr(
        service,
        "_evaluate_message_collaboration_policy",
        lambda event, **_kwargs: SimpleNamespace(
            should_start_turn=True,
            command_allowed=True,
            log_fields=lambda: {},
        ),
    )

    event = ChatMessageEvent(
        update_id="update-3",
        thread=ChatThreadRef(platform="discord", chat_id="channel-1", thread_id=None),
        message=ChatMessageRef(
            thread=ChatThreadRef(
                platform="discord", chat_id="channel-1", thread_id=None
            ),
            message_id="msg-3",
        ),
        from_user_id="user-1",
        text="follow up",
        reply_to=ChatMessageRef(
            thread=ChatThreadRef(
                platform="discord", chat_id="channel-1", thread_id=None
            ),
            message_id="reply-1",
        ),
    )

    await service._handle_message_event(event, build_dispatch_context(event))

    hydrated = captured.get("event")
    assert hydrated is not None
    assert hydrated.reply_context is not None
    assert hydrated.reply_context.text == "fetched bot reply"
    assert hydrated.reply_context.author_label == "Codex"
    assert hydrated.reply_context.is_bot is True


@pytest.mark.asyncio
async def test_discord_backend_approval_request_sends_prompt_and_accepts(
    tmp_path: Path,
) -> None:
    rest = _FakeRest()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        service._discord_turn_approval_contexts["turn-1"] = (
            discord_service_module._DiscordTurnApprovalContext(channel_id="channel-1")
        )
        request = {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "turnId": "turn-1",
                "reason": "Need permission",
                "command": ["/bin/zsh", "-c", "ps -p 123"],
            },
        }

        decision_task = asyncio.create_task(
            service._handle_backend_approval_request(request)
        )
        await asyncio.sleep(0)

        assert len(rest.channel_messages) == 1
        payload = rest.channel_messages[0]["payload"]
        assert "Approval required" in payload["content"]
        action_rows = payload["components"]
        accept_custom_id = action_rows[0]["components"][0]["custom_id"]

        await service._handle_component_interaction_normalized(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            custom_id=accept_custom_id,
            values=None,
            guild_id="guild-1",
            user_id="user-1",
        )

        assert await decision_task == "accept"
        assert rest.deleted_channel_messages == [
            {
                "channel_id": "channel-1",
                "message_id": rest.channel_messages[0]["message_id"],
            }
        ]
        assert (
            rest.interaction_responses[-1]["payload"]["data"]["content"]
            == "Decision: accept"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_discord_approval_component_falls_back_to_edit_when_delete_fails(
    tmp_path: Path,
) -> None:
    rest = _DeleteFailingRest()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        service._discord_pending_approvals["token-1"] = (
            discord_service_module._DiscordPendingApproval(
                token="token-1",
                request_id="approval-1",
                turn_id="turn-1",
                channel_id="channel-1",
                message_id="msg-1",
                prompt="Approval required",
                future=future,
            )
        )

        await service._handle_component_interaction_normalized(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            custom_id="approval:token-1:decline",
            values=None,
            guild_id="guild-1",
            user_id="user-1",
        )

        assert future.done() and future.result() == "decline"
        assert rest.edited_channel_messages == [
            {
                "channel_id": "channel-1",
                "message_id": "msg-1",
                "payload": {
                    "content": "Approval decline.",
                    "components": [],
                },
            }
        ]
    finally:
        await store.close()


def _config(
    root: Path,
    *,
    allow_user_ids: frozenset[str],
    command_registration_enabled: bool = True,
    command_scope: str = "guild",
    command_guild_ids: tuple[str, ...] = ("guild-1",),
    collaboration_policy: CollaborationPolicy | None = None,
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
        collaboration_policy=collaboration_policy,
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


def test_list_discord_thread_targets_for_picker_filters_by_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=None,
        outbox_manager=_FakeOutboxManager(),
    )
    orchestration_service = service._discord_thread_service()
    repo_thread = orchestration_service.create_thread_target(
        "codex",
        workspace,
        repo_id="repo-1",
        display_name="discord:repo",
    )
    pma_thread = orchestration_service.create_thread_target(
        "codex",
        workspace,
        repo_id="repo-1",
        display_name="discord:pma",
    )
    unbound_thread = orchestration_service.create_thread_target(
        "codex",
        workspace,
        repo_id="repo-1",
        display_name="discord:unbound",
    )
    orchestration_service.upsert_binding(
        surface_kind="discord",
        surface_key="repo-channel",
        thread_target_id=repo_thread.thread_target_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="repo",
        metadata={"channel_id": "repo-channel", "pma_enabled": False},
    )
    orchestration_service.upsert_binding(
        surface_kind="discord",
        surface_key="pma-channel",
        thread_target_id=pma_thread.thread_target_id,
        agent_id="codex",
        repo_id="repo-1",
        mode="pma",
        metadata={"channel_id": "pma-channel", "pma_enabled": True},
    )

    repo_items = service._list_discord_thread_targets_for_picker(
        workspace_root=workspace,
        agent="codex",
        current_thread_id=None,
        mode="repo",
    )
    pma_items = service._list_discord_thread_targets_for_picker(
        workspace_root=workspace,
        agent="codex",
        current_thread_id=None,
        mode="pma",
    )

    assert {thread_id for thread_id, _label in repo_items} == {
        repo_thread.thread_target_id,
        unbound_thread.thread_target_id,
    }
    assert {thread_id for thread_id, _label in pma_items} == {
        pma_thread.thread_target_id,
        unbound_thread.thread_target_id,
    }


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
    *, selected_value: str = "repo-1", user_id: str = "user-1"
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
            "values": [selected_value],
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


def test_session_thread_picker_label_prefers_preview_and_marks_current() -> None:
    label = discord_service_module._format_session_thread_picker_label(
        "019cc7c1-ec10-7981-8e8b-ec5db4619efb",
        {
            "id": "019cc7c1-ec10-7981-8e8b-ec5db4619efb",
            "last_user_message": "Fix resume picker so the options show summaries",
            "last_assistant_message": "I will update labels to include preview text",
        },
        is_current=True,
    )
    assert "(current)" in label
    assert "[019cc7c1]" in label
    assert "Fix resume picker so the options show summaries" in label


def test_session_thread_picker_label_falls_back_to_thread_id() -> None:
    thread_id = "019cc738-5168-7ca1-9d80-ab180b4b31dd"
    label = discord_service_module._format_session_thread_picker_label(
        thread_id,
        {"id": thread_id},
        is_current=False,
    )
    assert label == thread_id


def test_session_thread_picker_label_strips_injected_context_from_preview() -> None:
    thread_id = "019cc77b-ec10-7981-8e8b-ec5db4619efb"
    label = discord_service_module._format_session_thread_picker_label(
        thread_id,
        {
            "id": thread_id,
            "last_user_message": (
                "<injected context>\n"
                "You are operating inside a Codex Autorunner (CAR) managed repo.\n"
                "</injected context>\n\n"
                "Resume this thread"
            ),
        },
        is_current=False,
    )
    assert "<injected context>" not in label
    assert "Resume this thread" in label


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
        assert "policy mode:" in status_payload["data"]["content"].lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_status_reports_effective_collaboration_policy(
    tmp_path: Path,
) -> None:
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
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=(),
        allowed_user_ids=("user-1",),
        collaboration_raw={
            "default_mode": "command_only",
            "destinations": [
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "mode": "active",
                    "plain_text_trigger": "mentions",
                }
            ],
        },
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allow_user_ids=frozenset({"user-1"}),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        status_payload = rest.interaction_responses[1]["payload"]["data"]["content"]
        lowered = status_payload.lower()
        assert "policy mode: active" in lowered
        assert "policy plain-text trigger: mentions" in lowered
        assert "binding/pma: bound workspace" in lowered
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_ids_reports_collaboration_snippet(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="ids", options=[])])
    policy = build_discord_collaboration_policy(
        allowed_guild_ids=("guild-1",),
        allowed_channel_ids=(),
        allowed_user_ids=("user-1",),
        collaboration_raw={
            "default_mode": "command_only",
            "destinations": [
                {
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "mode": "active",
                    "plain_text_trigger": "mentions",
                }
            ],
        },
    )
    service = DiscordBotService(
        _config(
            tmp_path,
            allow_user_ids=frozenset({"user-1"}),
            collaboration_policy=policy,
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Suggested collaboration config:" in content
        assert "default_mode: command_only" in content
        assert "channel_id: channel-1" in content
        assert "mode: silent" in content
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
async def test_service_bind_partial_workspace_value_returns_filtered_picker(
    tmp_path: Path,
) -> None:
    stablecoin_workspace = tmp_path / "worktrees" / "stablecoin-engine"
    stablecoin_workspace.mkdir(parents=True)
    repos = [
        ("stablecoin-engine", str(stablecoin_workspace)),
        ("ios-app-template", str(tmp_path / "repos" / "ios-app-template")),
    ]
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": "engine"}],
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
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 1 workspaces" in content
        menu = payload["data"]["components"][0]["components"][0]
        option = menu["options"][0]
        assert option["label"] == "stablecoin-engine"
        assert option["value"] == "stablecoin-engine"
        assert option["description"] == str(stablecoin_workspace.resolve())[:100]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_bind_partial_workspace_value_returns_path_candidate_picker(
    tmp_path: Path,
) -> None:
    engine_workspace = tmp_path / "engine-room"
    engine_workspace.mkdir()
    misc_workspace = tmp_path / "misc"
    misc_workspace.mkdir()
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="bind",
                options=[{"type": 3, "name": "workspace", "value": "engine"}],
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
    service._list_bind_workspace_candidates = lambda: [  # type: ignore[assignment]
        (None, None, str(engine_workspace.resolve())),
        (None, None, str(misc_workspace.resolve())),
    ]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 1 workspaces" in content
        menu = payload["data"]["components"][0]["components"][0]
        option = menu["options"][0]
        assert option["label"] == "engine-room"
        assert option["value"] == workspace_autocomplete_value(
            str(engine_workspace.resolve())
        )
        assert option["description"] == str(engine_workspace.resolve())[:100]
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
async def test_service_routes_bind_picker_component_interaction_for_path_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "engine-room"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_bind_select_interaction(selected_value=str(workspace.resolve()))]
    )
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._list_bind_workspace_candidates = lambda: [  # type: ignore[assignment]
        (None, None, str(workspace.resolve()))
    ]

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] is None
        assert binding["workspace_path"] == str(workspace.resolve())

        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "bound this channel to workspace" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_bind_picker_component_interaction_for_tokenized_path_candidate(
    tmp_path: Path,
) -> None:
    long_dir = "workspace-" + ("x" * 120)
    workspace = tmp_path / long_dir
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _bind_select_interaction(
                selected_value=workspace_autocomplete_value(str(workspace.resolve()))
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
    service._list_bind_workspace_candidates = lambda: [  # type: ignore[assignment]
        (None, None, str(workspace.resolve()))
    ]

    try:
        await service.run_forever()
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding["repo_id"] is None
        assert binding["workspace_path"] == str(workspace.resolve())
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_model_name_autocomplete_returns_filtered_models(
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
            _autocomplete_interaction(
                name="model",
                focused_name="name",
                focused_value="glm",
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

    class _ModelClient:
        async def model_list(self, **kwargs: Any) -> Any:
            _ = kwargs
            return {
                "data": [
                    {"id": "openai/gpt-5"},
                    {"id": "zai/glm-5"},
                ]
            }

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _ModelClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        assert [entry["value"] for entry in payload["data"]["choices"]] == ["zai/glm-5"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_skills_search_autocomplete_returns_filtered_skills(
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
            _autocomplete_interaction(
                name="skills",
                focused_name="search",
                focused_value="deck",
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

    class _SkillsClient:
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            assert method == "skills/list"
            assert params["cwds"] == [str(workspace)]
            return [
                {
                    "cwd": str(workspace),
                    "skills": [
                        {
                            "name": "slides",
                            "shortDescription": "Build deck presentations",
                        },
                        {
                            "name": "spreadsheets",
                            "shortDescription": "Work with tables",
                        },
                    ],
                }
            ]

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _SkillsClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        assert [entry["value"] for entry in payload["data"]["choices"]] == ["slides"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_tickets_search_autocomplete_returns_filtered_tickets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_discord_alpha1"\nagent: codex\ntitle: Alpha task\ndone: false\n---\n\nBody\n',
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        '---\nticket_id: "tkt_discord_beta1"\nagent: codex\ntitle: Beta task\ndone: true\n---\n\nBody\n',
        encoding="utf-8",
    )
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
            _autocomplete_interaction(
                name="tickets",
                focused_name="search",
                focused_value="beta",
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
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        assert [entry["value"] for entry in payload["data"]["choices"]] == [
            ".codex-autorunner/tickets/TICKET-002.md"
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_session_resume_autocomplete_filters_threads(
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
                command_path=("car", "session", "resume"),
                focused_name="thread_id",
                focused_value="def",
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

    async def _fake_list_session_threads_for_picker(
        *,
        workspace_root: Path,
        current_thread_id: str | None,
    ) -> list[tuple[str, str]]:
        _ = workspace_root, current_thread_id
        return [
            ("thread-abc", "thread-abc"),
            ("thread-def", "thread-def"),
            ("thread-xyz", "thread-xyz"),
        ]

    service._list_session_threads_for_picker = (  # type: ignore[assignment]
        _fake_list_session_threads_for_picker
    )

    try:
        await service.run_forever()
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 8
        assert [entry["value"] for entry in payload["data"]["choices"]] == [
            "thread-def"
        ]
    finally:
        await store.close()


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
    token = repo_autocomplete_value(long_repo_id)
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
    gateway = _FakeGateway([_bind_select_interaction(selected_value="repo-1")])
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
    await store.update_model_state(
        channel_id="channel-1",
        model_override="zai-coding-plan/glm-5",
        reasoning_effort="high",
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
        assert binding.get("model_override") is None
        assert binding.get("reasoning_effort") is None
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
async def test_car_model_without_name_uses_opencode_catalog_for_opencode_agent(
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
    await store.update_agent_state(channel_id="channel-1", agent="opencode")
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

    class _FakeOpenCodeClient:
        async def providers(self, *, directory: str | None = None) -> Any:
            _ = directory
            return {
                "providers": [
                    {
                        "id": "openai",
                        "models": {
                            "gpt-4o": {"name": "GPT-4o"},
                        },
                    }
                ]
            }

    class _FakeOpenCodeSupervisor:
        async def get_client(self, _workspace_root: Path) -> Any:
            return _FakeOpenCodeClient()

    async def _fake_opencode_supervisor_for_workspace(_workspace_root: Path) -> Any:
        return _FakeOpenCodeSupervisor()

    async def _unexpected_client_for_workspace(_workspace_path: str) -> Any:
        raise AssertionError("Codex app-server client should not be used for opencode")

    service._opencode_supervisor_for_workspace = (  # type: ignore[assignment]
        _fake_opencode_supervisor_for_workspace
    )
    service._client_for_workspace = _unexpected_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        data = rest.followup_messages[0]["payload"]
        assert "current agent: opencode" in data["content"].lower()
        components = data.get("components") or []
        assert components
        menu = components[0]["components"][0]
        assert menu["custom_id"] == "model_select"
        values = [opt["value"] for opt in menu["options"]]
        assert "openai/gpt-4o" in values
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
async def test_car_model_with_partial_name_prompts_filtered_picker(
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
                name="model",
                options=[{"name": "name", "value": "glm"}],
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

    class _ModelClient:
        async def model_list(self, **kwargs: Any) -> Any:
            _ = kwargs
            return {
                "data": [
                    {"id": "zai/glm-4.5"},
                    {"id": "zai/glm-5"},
                    {"id": "openai/gpt-5"},
                ]
            }

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _ModelClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 2 models" in content
        select = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["clear", "zai/glm-4.5", "zai/glm-5"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_resume_with_partial_thread_prompts_filtered_picker(
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
                command_path=("car", "session", "resume"),
                options=[{"name": "thread_id", "value": "def"}],
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

    def _fake_list_threads(
        *,
        workspace_root: Path,
        agent: str,
        current_thread_id: str | None,
        mode: str,
        repo_id: str | None = None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        limit: int = 25,
    ) -> list[tuple[str, str]]:
        _ = (
            workspace_root,
            agent,
            current_thread_id,
            mode,
            repo_id,
            resource_kind,
            resource_id,
            limit,
        )
        return [
            ("thread-abc", "thread-abc"),
            ("thread-def", "thread-def"),
            ("thread-xyz", "thread-xyz"),
        ]

    service._list_discord_thread_targets_for_picker = _fake_list_threads  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 2
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "matched 1 threads" in content
        select = rest.followup_messages[1]["payload"]["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["thread-def"]
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
    gateway = _FakeGateway(
        [
            _interaction_path(
                command_path=("car", "flow", "resume"),
                options=[{"name": "run_id", "value": "run"}],
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
                _Run("run-alpha", FlowRunStatus.PAUSED),
                _Run("run-beta", FlowRunStatus.PAUSED),
            ]

        def close(self) -> None:
            return None

    service._open_flow_store = lambda _workspace_root: _Store()  # type: ignore[assignment]

    try:
        await service.run_forever()
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 2 runs" in content
        select = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["run-alpha", "run-beta"]
    finally:
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
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 2 runs" in content
        select = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in select["options"]]
        assert values == ["run-paused-a", "run-paused-b"]
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
        [_component_interaction(custom_id="model_select", values=["openai/gpt-4o"])]
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
        assert binding.get("model_override") == "openai/gpt-4o"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "model set to openai/gpt-4o" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_model_rejects_invalid_opencode_model_name(tmp_path: Path) -> None:
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
        [
            _interaction(
                name="model",
                options=[{"name": "name", "value": "gpt-5.3-codex-unknown"}],
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
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("model_override") is None
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "provider/model" in content
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

    async def _list_model_items_for_binding_stub(
        **_kwargs: Any,
    ) -> list[tuple[str, str]]:
        return [("gpt-5.3-codex", "gpt-5.3-codex")]

    monkeypatch.setattr(
        service,
        "_list_model_items_for_binding",
        _list_model_items_for_binding_stub,
    )

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
            values=["openai/gpt-4o"],
        )
        context = build_dispatch_context(event)
        await service._handle_normalized_interaction(event, context)
        binding = await store.get_binding(channel_id="channel-1")
        assert binding is not None
        assert binding.get("model_override") == "openai/gpt-4o"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "model set to openai/gpt-4o" in content
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

    def _fake_list_threads(
        *,
        workspace_root: Path,
        agent: str,
        current_thread_id: str | None,
        mode: str,
        repo_id: str | None = None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        limit: int = 25,
    ) -> list[tuple[str, str]]:
        _ = (
            workspace_root,
            agent,
            current_thread_id,
            mode,
            repo_id,
            resource_kind,
            resource_id,
            limit,
        )
        return [("thread-1", "thread-1 (current)"), ("thread-2", "thread-2")]

    service._list_discord_thread_targets_for_picker = _fake_list_threads  # type: ignore[assignment]

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
async def test_car_review_partial_commit_value_returns_filtered_picker(
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
                options=[{"type": 3, "name": "target", "value": "commit fix"}],
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
        return [
            ("abcdef1234567890", "Fix picker"),
            ("0123456789abcdef", "Fix search fallback"),
            ("fedcba9876543210", "Refactor tests"),
        ]

    service._list_recent_commits_for_picker = _fake_list_recent_commits  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        content = payload["data"]["content"].lower()
        assert "matched 2 commits" in content
        menu = payload["data"]["components"][0]["components"][0]
        values = [option["value"] for option in menu["options"]]
        assert values == ["abcdef1234567890", "0123456789abcdef"]
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
async def test_car_tickets_returns_ticket_picker_components(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_discord_first"\ntitle: First\ndone: false\n---\n\nBody\n',
        encoding="utf-8",
    )
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="tickets", options=[])])
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
        assert data["content"] == "Select a ticket to view or edit."
        components = data.get("components") or []
        assert [row["components"][0]["custom_id"] for row in components] == [
            "tickets_filter_select",
            "tickets_select",
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_tickets_preserves_long_ticket_paths_via_picker_token(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    long_name = f"TICKET-001-{'x' * 120}.md"
    ticket_rel = f".codex-autorunner/tickets/{long_name}"
    (ticket_dir / long_name).write_text(
        '---\nticket_id: "tkt_discord_long1"\nagent: codex\ntitle: Very long ticket\ndone: false\n---\n\nBody\n',
        encoding="utf-8",
    )
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="tickets", options=[])])
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
        picker_data = rest.interaction_responses[0]["payload"]["data"]
        picker_options = picker_data["components"][1]["components"][0]["options"]
        assert len(picker_options) == 1
        option_value = picker_options[0]["value"]
        assert option_value.startswith("ticket@")
        assert len(option_value) <= 100

        rest.interaction_responses.clear()
        gateway._events = [
            _component_interaction(
                custom_id="tickets_select",
                values=[option_value],
            )
        ]
        await service.run_forever()

        modal_payload = rest.interaction_responses[0]["payload"]
        assert modal_payload["type"] == 9
        text_input = modal_payload["data"]["components"][0]["component"]
        assert text_input["value"] == (
            '---\nticket_id: "tkt_discord_long1"\nagent: codex\ntitle: Very long ticket\ndone: false\n---\n\nBody\n'
        )
        assert (
            service._resolve_ticket_picker_value(
                option_value,
                workspace_root=workspace,
            )
            == ticket_rel
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_tickets_search_filters_picker_and_persists_across_filter_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_discord_alpha2"\nagent: codex\ntitle: Alpha task\ndone: false\n---\n\nBody\n',
        encoding="utf-8",
    )
    (ticket_dir / "TICKET-002.md").write_text(
        '---\nticket_id: "tkt_discord_beta2"\nagent: codex\ntitle: Beta task\ndone: true\n---\n\nBody\n',
        encoding="utf-8",
    )
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
                name="tickets",
                options=[{"type": 3, "name": "search", "value": "beta"}],
            ),
            _component_interaction(
                custom_id="tickets_filter_select",
                values=["done"],
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
        initial_data = rest.interaction_responses[0]["payload"]["data"]
        initial_options = initial_data["components"][1]["components"][0]["options"]
        assert [option["value"] for option in initial_options] == [
            ".codex-autorunner/tickets/TICKET-002.md"
        ]

        filtered_data = rest.interaction_responses[1]["payload"]["data"]
        assert "Search: `beta`" in filtered_data["content"]
        filtered_options = filtered_data["components"][1]["components"][0]["options"]
        assert [option["value"] for option in filtered_options] == [
            ".codex-autorunner/tickets/TICKET-002.md"
        ]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_skills_search_filters_results(tmp_path: Path) -> None:
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
                name="skills",
                options=[{"type": 3, "name": "search", "value": "deck"}],
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

    class _SkillsClient:
        async def request(self, method: str, params: dict[str, Any]) -> Any:
            assert method == "skills/list"
            assert params["cwds"] == [str(workspace)]
            return [
                {
                    "cwd": str(workspace),
                    "skills": [
                        {
                            "name": "slides",
                            "shortDescription": "Build deck presentations",
                        },
                        {
                            "name": "spreadsheets",
                            "shortDescription": "Work with tables",
                        },
                    ],
                }
            ]

    async def _fake_client_for_workspace(_workspace_path: str) -> Any:
        return _SkillsClient()

    service._client_for_workspace = _fake_client_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Skills matching `deck`" in content
        assert "slides" in content
        assert "spreadsheets" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_ticket_picker_rejects_modal_for_too_large_ticket(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    oversized_body = "x" * 4001
    ticket_path.write_text(oversized_body, encoding="utf-8")
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
                custom_id="tickets_select",
                values=[".codex-autorunner/tickets/TICKET-001.md"],
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
        assert payload["data"]["content"].startswith(
            "`" + ".codex-autorunner/tickets/TICKET-001.md" + "` is too large to edit"
        )
        assert ticket_path.read_text(encoding="utf-8") == oversized_body
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

    async def _should_not_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy BackendOrchestrator should not be used")

    service._orchestrator_for_workspace = _should_not_build_orchestrator  # type: ignore[assignment]

    try:
        await service.run_forever()
        _orch, binding_row, thread = service._get_discord_thread_binding(
            channel_id="channel-1",
            mode="repo",
        )
        assert binding_row is not None
        assert thread is not None
        assert thread.thread_target_id == binding_row.thread_target_id
        assert thread.lifecycle_status == "active"
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

    async def _should_not_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy BackendOrchestrator should not be used")

    service._orchestrator_for_workspace = _should_not_build_orchestrator  # type: ignore[assignment]

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
        _orch, binding_row, thread = service._get_discord_thread_binding(
            channel_id="channel-1",
            mode="repo",
        )
        assert binding_row is not None
        assert thread is not None
        assert thread.thread_target_id == binding_row.thread_target_id
        assert thread.lifecycle_status == "active"
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

    async def _should_not_build_orchestrator(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy BackendOrchestrator should not be used")

    service._orchestrator_for_workspace = _should_not_build_orchestrator  # type: ignore[assignment]

    try:
        await service.run_forever()
        _orch, binding_row, thread = service._get_discord_thread_binding(
            channel_id="channel-1",
            mode="pma",
        )
        assert binding_row is not None
        assert thread is not None
        assert thread.thread_target_id == binding_row.thread_target_id
        assert thread.lifecycle_status == "active"
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "fresh pma session" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_session_resume_reactivates_thread_without_backend_rebinding(
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

    resumed_calls: list[tuple[str, dict[str, Any]]] = []
    archived_thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="codex",
        workspace_root=str(workspace),
        lifecycle_status="archived",
        backend_thread_id="legacy-backend",
    )

    class _FakeThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return SimpleNamespace(thread_target_id="thread-1", mode="repo")

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return archived_thread

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            resumed_calls.append((thread_target_id, kwargs))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                agent_id="codex",
                workspace_root=str(workspace),
                lifecycle_status="active",
                backend_thread_id="legacy-backend",
            )

    service._get_discord_thread_binding = (  # type: ignore[assignment]
        lambda *args, **kwargs: (
            _FakeThreadService(),
            SimpleNamespace(thread_target_id="thread-1", mode="repo"),
            archived_thread,
        )
    )
    service._attach_discord_thread_binding = lambda *args, **kwargs: None  # type: ignore[assignment]
    service._list_discord_thread_targets_for_picker = (  # type: ignore[assignment]
        lambda *args, **kwargs: []
    )

    try:
        await service._handle_car_resume(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
            options={"thread_id": "thread-1"},
        )
        assert resumed_calls == [("thread-1", {})]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_interrupt_uses_orchestration_thread_state(tmp_path: Path) -> None:
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

    interrupted: list[str] = []

    class _FakeThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return SimpleNamespace(thread_target_id="thread-1", mode="repo")

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return SimpleNamespace(thread_target_id="thread-1")

        async def stop_thread(self, thread_target_id: str) -> Any:
            interrupted.append(thread_target_id)
            return SimpleNamespace(
                interrupted_active=True,
                recovered_lost_backend=False,
                cancelled_queued=2,
            )

    service._discord_thread_service = lambda: _FakeThreadService()  # type: ignore[assignment]

    try:
        await service._handle_car_interrupt(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        assert interrupted == ["thread-1"]
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "stopping current turn" in content
        assert "cancelled 2 queued turn" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_interrupt_recovers_missing_backend_thread(tmp_path: Path) -> None:
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

    class _FakeThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return SimpleNamespace(thread_target_id="thread-1", mode="repo")

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return SimpleNamespace(thread_target_id="thread-1")

        async def stop_thread(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return SimpleNamespace(
                interrupted_active=False,
                recovered_lost_backend=True,
                cancelled_queued=0,
            )

    service._discord_thread_service = lambda: _FakeThreadService()  # type: ignore[assignment]

    try:
        await service._handle_car_interrupt(
            "interaction-1",
            "token-1",
            channel_id="channel-1",
        )
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "recovered stale session" in content
        assert "backend thread was lost" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_reset_discord_thread_binding_archives_after_lost_backend_recovery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

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

    calls: list[tuple[str, str]] = []

    class _FakeThreadService:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            assert surface_kind == "discord"
            assert surface_key == "channel-1"
            return SimpleNamespace(thread_target_id="thread-1", mode="repo")

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return SimpleNamespace(thread_target_id="thread-1")

        async def stop_thread(self, thread_target_id: str) -> Any:
            calls.append(("stop", thread_target_id))
            return SimpleNamespace(recovered_lost_backend=True)

        def archive_thread_target(self, thread_target_id: str) -> None:
            calls.append(("archive", thread_target_id))

        def create_thread_target(
            self, agent: str, workspace_root: Path, **kwargs: Any
        ) -> Any:
            calls.append(("create", agent))
            assert workspace_root == workspace
            return SimpleNamespace(thread_target_id="thread-2")

        def upsert_binding(self, **kwargs: Any) -> Any:
            calls.append(("bind", str(kwargs["thread_target_id"])))
            return SimpleNamespace(thread_target_id=kwargs["thread_target_id"])

    service._discord_thread_service = lambda: _FakeThreadService()  # type: ignore[assignment]

    try:
        had_previous, new_thread_id = await service._reset_discord_thread_binding(
            channel_id="channel-1",
            workspace_root=workspace,
            agent="codex",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            pma_enabled=False,
        )
    finally:
        await store.close()

    assert had_previous is True
    assert new_thread_id == "thread-2"
    assert calls == [
        ("stop", "thread-1"),
        ("archive", "thread-1"),
        ("create", "codex"),
        ("bind", "thread-2"),
    ]


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
                options=[{"type": 3, "name": "target", "value": "all"}],
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
        assert observed["update_target"] == "all"
        assert observed["repo_ref"] == "main"
        assert "codex-autorunner.git" in observed["repo_url"]
        assert observed["notify_platform"] == "discord"
        assert observed["notify_context"] == {"chat_id": "channel-1"}
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "update started (all)" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_prompts_for_confirmation_when_sessions_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="update",
                options=[{"type": 3, "name": "target", "value": "all"}],
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

    service._active_update_session_count = lambda: 1  # type: ignore[method-assign]
    spawned = False

    def _fake_spawn_update_process(**_kwargs: Any) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(
        discord_service_module,
        "_spawn_update_process",
        _fake_spawn_update_process,
    )

    try:
        await service.run_forever()
        assert spawned is False
        assert len(rest.interaction_responses) == 1
        data = rest.interaction_responses[0]["payload"]["data"]
        assert "active codex session" in data["content"].lower()
        components = data.get("components") or []
        assert components
        buttons = components[0]["components"]
        assert buttons[0]["custom_id"] == "update_confirm:all"
        assert buttons[1]["custom_id"] == "update_cancel:all"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_component_interaction_update_cancel_reports_cancelled(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_component_interaction(custom_id="update_cancel:all", values=["all"])]
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
        assert "update cancelled" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_car_update_web_target_skips_confirmation_when_sessions_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _interaction(
                name="update",
                options=[{"type": 3, "name": "target", "value": "web"}],
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
    service._active_update_session_count = lambda: 1  # type: ignore[method-assign]

    try:
        await service.run_forever()
        assert observed["update_target"] == "web"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"].lower()
        assert "update started (web only)" in content
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


@pytest.mark.anyio
async def test_car_new_ignores_legacy_backend_factory(tmp_path: Path) -> None:
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
        backend_orchestrator_factory=lambda _workspace_root: (_ for _ in ()).throw(
            AssertionError("legacy backend factory should not be used")
        ),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "fresh repo session" in content
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


@pytest.mark.anyio
async def test_handle_chat_event_message_starts_and_stops_typing_indicator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_handle_message_event(_event: Any, _context: Any) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(service, "_handle_message_event", _fake_handle_message_event)

    event = ChatMessageEvent(
        update_id="u-1",
        thread=ChatThreadRef(platform="discord", chat_id="channel-1", thread_id=None),
        message=ChatMessageRef(
            thread=ChatThreadRef(
                platform="discord",
                chat_id="channel-1",
                thread_id=None,
            ),
            message_id="m-1",
        ),
        from_user_id="user-1",
        text="hello",
    )
    context = build_dispatch_context(event)
    task = asyncio.create_task(service._handle_chat_event(event, context))
    try:
        await started.wait()
        for _ in range(100):
            if rest.typing_calls:
                break
            await asyncio.sleep(0.01)
        assert rest.typing_calls == ["channel-1"]
        release.set()
        await task
        assert not await service._typing_session_active("channel-1")
    finally:
        release.set()
        await store.close()


@pytest.mark.anyio
async def test_typing_indicator_reference_count_waits_for_last_end(
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
        await service._begin_typing_indicator("channel-1")
        await service._begin_typing_indicator("channel-1")
        assert await service._typing_session_active("channel-1")
        await service._end_typing_indicator("channel-1")
        assert await service._typing_session_active("channel-1")
        await service._end_typing_indicator("channel-1")
        assert not await service._typing_session_active("channel-1")
    finally:
        await store.close()


@pytest.mark.anyio
async def test_run_agent_turn_for_message_wraps_typing_indicator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run_agent_turn(
        _service: Any,
        *,
        workspace_root: Path,
        prompt_text: str,
        input_items: list[dict[str, Any]] | None = None,
        agent: str,
        model_override: str | None,
        reasoning_effort: str | None,
        session_key: str,
        orchestrator_channel_key: str,
        max_actions: int,
        min_edit_interval_seconds: float,
        heartbeat_interval_seconds: float,
        log_event_fn: Any,
    ) -> DiscordMessageTurnResult:
        _ = (
            workspace_root,
            prompt_text,
            input_items,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
            max_actions,
            min_edit_interval_seconds,
            heartbeat_interval_seconds,
            log_event_fn,
        )
        started.set()
        await release.wait()
        return DiscordMessageTurnResult(final_message="ok")

    monkeypatch.setattr(
        discord_service_module,
        "run_agent_turn_for_message",
        _fake_run_agent_turn,
    )

    task = asyncio.create_task(
        service._run_agent_turn_for_message(
            workspace_root=tmp_path,
            prompt_text="hello",
            agent="codex",
            model_override=None,
            reasoning_effort=None,
            session_key="session-1",
            orchestrator_channel_key="channel-1",
        )
    )
    try:
        await started.wait()
        for _ in range(100):
            if rest.typing_calls:
                break
            await asyncio.sleep(0.01)
        assert rest.typing_calls == ["channel-1"]
        release.set()
        result = await task
        assert result.final_message == "ok"
        assert not await service._typing_session_active("channel-1")
    finally:
        release.set()
        await store.close()
