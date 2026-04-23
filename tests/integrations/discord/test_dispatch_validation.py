from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationPolicy,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.errors import DiscordAPIError
from codex_autorunner.integrations.discord.service import (
    DiscordBotService,
)
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


async def _dispatch_gateway_interaction(
    service: DiscordBotService,
    payload: dict[str, Any],
) -> None:
    await service._on_dispatch("INTERACTION_CREATE", payload)
    await asyncio.wait_for(service._command_runner.shutdown(), timeout=3.0)


@pytest.mark.anyio
async def test_rejected_interaction_skips_submission_order(tmp_path: Path) -> None:
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
    skip_submission_order = MagicMock()
    service._command_runner.skip_submission_order = (  # type: ignore[method-assign]
        skip_submission_order
    )

    try:
        payload = _interaction(
            name="bind",
            options=[{"type": 3, "name": "workspace", "value": str(tmp_path)}],
            user_id="unauthorized",
        )
        payload["__car_dispatch_order"] = 7
        await service._on_dispatch("INTERACTION_CREATE", payload)
        skip_submission_order.assert_called_once_with(7)
    finally:
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
async def test_pre_submit_failure_skips_submission_order(tmp_path: Path) -> None:
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
    skip_submission_order = MagicMock()
    service._command_runner.skip_submission_order = (  # type: ignore[method-assign]
        skip_submission_order
    )
    service._persist_runtime_interaction = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("persist failed")
    )

    try:
        payload = _interaction(name="status", options=[])
        payload["__car_dispatch_order"] = 8
        with pytest.raises(RuntimeError, match="persist failed"):
            await service._on_dispatch("INTERACTION_CREATE", payload)
        skip_submission_order.assert_called_once_with(8)
    finally:
        await service._shutdown()
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
        assert payload["type"] == 5
        assert len(rest.followup_messages) == 1
        assert "not bound" in rest.followup_messages[0]["payload"]["content"].lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_attempts_fallback_reply_when_initial_response_fails(
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
        assert (
            rest.followup_messages[0]["payload"]["content"]
            == "Discord interaction did not acknowledge. Please retry."
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_slash_command_timeout_followup_through_runner(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="status", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._command_runner._config = replace(
        service._command_runner._config,
        timeout_seconds=0.01,
        stalled_warning_seconds=None,
    )

    async def _slow_handle_car_command(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    service._handle_car_command = _slow_handle_car_command  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        assert "timed out" in rest.followup_messages[0]["payload"]["content"].lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_service_routes_slash_command_error_followup_through_runner(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([_interaction(name="status", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _failing_handle_car_command(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    service._handle_car_command = _failing_handle_car_command  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert len(rest.followup_messages) == 1
        content = rest.followup_messages[0]["payload"]["content"].lower()
        assert "unexpected error" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_on_dispatch_backgrounds_interaction_handling(
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
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_handle_car_command(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        await release.wait()

    service._handle_car_command = _slow_handle_car_command  # type: ignore[assignment]

    try:
        dispatch_task = asyncio.create_task(
            service._on_dispatch(
                "INTERACTION_CREATE",
                _interaction_path(
                    command_path=("car", "session", "compact"),
                    options=[],
                ),
            )
        )
        await asyncio.wait_for(dispatch_task, timeout=3.0)
        assert len(rest.interaction_responses) == 1
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        await asyncio.wait_for(started.wait(), timeout=3.0)
        release.set()
        await asyncio.wait_for(service._command_runner.shutdown(), timeout=3.0)
    finally:
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "payload",
        "expected_dispatch_ack_policy",
        "expected_conversation",
        "expected_resource_keys",
        "expected_queue_wait_ack_policy",
    ),
    [
        (
            _interaction_path(
                command_path=("car", "session", "compact"),
                options=[],
            ),
            None,
            "discord:channel-1:guild-1",
            ("conversation:discord:channel-1:guild-1",),
            None,
        ),
        (
            _component_interaction(
                custom_id="approval:abc:approve",
                values=None,
            ),
            None,
            "discord:channel-1:guild-1",
            ("conversation:discord:channel-1:guild-1",),
            "defer_component_update",
        ),
        (
            _component_interaction(
                custom_id="flow:run-1:restart",
                values=None,
            ),
            "defer_component_update",
            "discord:channel-1:guild-1",
            ("conversation:discord:channel-1:guild-1",),
            "defer_component_update",
        ),
        (
            {
                "id": "inter-modal-1",
                "token": "token-modal-1",
                "channel_id": "channel-1",
                "guild_id": "guild-1",
                "type": 5,
                "member": {"user": {"id": "user-1"}},
                "data": {
                    "custom_id": "tickets_modal:abc",
                    "components": [
                        {
                            "type": 18,
                            "label": "Ticket",
                            "component": {
                                "type": 4,
                                "custom_id": "ticket_body",
                                "value": "body text",
                            },
                        }
                    ],
                },
            },
            None,
            "discord:channel-1:guild-1",
            ("conversation:discord:channel-1:guild-1",),
            "defer_ephemeral",
        ),
        (
            _autocomplete_interaction_path(
                command_path=("car", "bind"),
                focused_name="workspace",
                focused_value="codex",
            ),
            None,
            None,
            (),
            None,
        ),
    ],
)
async def test_on_dispatch_routes_interactions_through_scheduler(
    tmp_path: Path,
    payload: dict[str, Any],
    expected_dispatch_ack_policy: str | None,
    expected_conversation: str | None,
    expected_resource_keys: tuple[str, ...],
    expected_queue_wait_ack_policy: str | None,
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
    submit = MagicMock()
    service._command_runner.submit = submit  # type: ignore[method-assign]

    try:
        await service._on_dispatch("INTERACTION_CREATE", payload)
        submit.assert_called_once()
        kwargs = submit.call_args.kwargs
        assert kwargs["conversation_id"] == expected_conversation
        assert kwargs["resource_keys"] == expected_resource_keys
        assert kwargs["queue_wait_ack_policy"] == expected_queue_wait_ack_policy
        if expected_dispatch_ack_policy is not None:
            assert len(rest.interaction_responses) == 1
            assert rest.interaction_responses[0]["payload"]["type"] == 6
    finally:
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
async def test_autocomplete_bypasses_busy_ingressed_fifo(
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
    slash_started = asyncio.Event()
    slash_release = asyncio.Event()
    autocomplete_started = asyncio.Event()

    async def _slow_handle_car_command(*_args: Any, **_kwargs: Any) -> None:
        slash_started.set()
        await slash_release.wait()

    async def _fast_autocomplete(*_args: Any, **_kwargs: Any) -> None:
        autocomplete_started.set()

    service._handle_car_command = _slow_handle_car_command  # type: ignore[assignment]
    service._handle_command_autocomplete = _fast_autocomplete  # type: ignore[assignment]

    try:
        await service._on_dispatch(
            "INTERACTION_CREATE",
            _interaction_path(
                command_path=("car", "session", "compact"),
                options=[],
            ),
        )
        await asyncio.wait_for(slash_started.wait(), timeout=1.0)

        await service._on_dispatch(
            "INTERACTION_CREATE",
            _autocomplete_interaction_path(
                command_path=("car", "bind"),
                focused_name="workspace",
                focused_value="codex",
            ),
        )
        await asyncio.wait_for(autocomplete_started.wait(), timeout=1.0)
    finally:
        slash_release.set()
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
async def test_scheduler_serializes_workspace_mutations_across_channels(
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
    await store.upsert_binding(
        channel_id="channel-2",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id="repo-1",
    )

    rest = _FakeRest()
    config = replace(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        allowed_channel_ids=frozenset({"channel-1", "channel-2"}),
    )
    service = DiscordBotService(
        config,
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    slash_started = asyncio.Event()
    slash_release = asyncio.Event()
    component_started = asyncio.Event()
    start_order: list[str] = []

    async def _slow_car_new(*_args: Any, **_kwargs: Any) -> None:
        start_order.append("slash")
        slash_started.set()
        await slash_release.wait()

    async def _flow_restart(*_args: Any, **_kwargs: Any) -> None:
        start_order.append("component")
        component_started.set()

    service._handle_car_new = _slow_car_new  # type: ignore[assignment]
    service._handle_flow_restart = _flow_restart  # type: ignore[assignment]

    try:
        await service._on_dispatch(
            "INTERACTION_CREATE",
            _interaction_path(
                command_path=("car", "new"),
                options=[],
            ),
        )
        await asyncio.wait_for(slash_started.wait(), timeout=1.0)

        await service._on_dispatch(
            "INTERACTION_CREATE",
            {
                "id": "inter-component-2",
                "token": "token-component-2",
                "channel_id": "channel-2",
                "guild_id": "guild-1",
                "type": 3,
                "member": {"user": {"id": "user-1"}},
                "data": {
                    "component_type": 3,
                    "custom_id": "flow_action_select:restart",
                    "values": ["run-1"],
                },
            },
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(component_started.wait(), timeout=0.1)

        slash_release.set()
        await asyncio.wait_for(component_started.wait(), timeout=3.0)
        assert start_order == ["slash", "component"]
    finally:
        slash_release.set()
        await service._shutdown()
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
async def test_dispatch_deferred_slash_commands_ack_before_prior_handler_finishes(
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

    first = _interaction(name="newt", options=[])
    first["id"] = "inter-1"
    first["token"] = "token-1"
    second = _interaction(name="newt", options=[])
    second["id"] = "inter-2"
    second["token"] = "token-2"

    rest = _FakeRest()
    gateway = _FakeGateway([first, second])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    started: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _fake_handle_newt(
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: str | None,
    ) -> None:
        _ = interaction_token, channel_id, guild_id
        started.append(interaction_id)
        if interaction_id == "inter-1":
            first_started.set()
            await release_first.wait()

    service._handle_car_newt = _fake_handle_newt  # type: ignore[assignment]

    task = asyncio.create_task(service.run_forever())
    try:
        for _ in range(50):
            if len(rest.interaction_responses) >= 2:
                break
            await asyncio.sleep(0.01)

        assert [item["interaction_id"] for item in rest.interaction_responses] == [
            "inter-1",
            "inter-2",
        ]
        assert [item["payload"]["type"] for item in rest.interaction_responses] == [
            5,
            5,
        ]
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        assert started == ["inter-1"]

        release_first.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert started == ["inter-1", "inter-2"]
    finally:
        release_first.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await store.close()


@pytest.mark.anyio
async def test_message_turn_waits_for_ingressed_slash_command_to_finish(
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

    release_newt = asyncio.Event()
    message_turn_started = asyncio.Event()
    observed: list[str] = []

    async def _fake_handle_newt(
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: str | None,
    ) -> None:
        _ = interaction_id, interaction_token, channel_id, guild_id
        observed.append("newt:start")
        await release_newt.wait()
        observed.append("newt:end")

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        input_items: list[dict[str, Any]] | None = None,
        source_message_id: str | None = None,
        agent: str,
        model_override: str | None,
        reasoning_effort: str | None,
        session_key: str,
        orchestrator_channel_key: str,
        managed_thread_surface_key: str | None = None,
        chat_ux_snapshot: Any = None,
    ) -> Any:
        _ = (
            workspace_root,
            input_items,
            source_message_id,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
            managed_thread_surface_key,
            chat_ux_snapshot,
        )
        observed.append(f"message:{prompt_text}")
        message_turn_started.set()
        return SimpleNamespace(final_message="message reply")

    service._handle_car_newt = _fake_handle_newt  # type: ignore[assignment]
    service._run_agent_turn_for_message = _fake_run_turn.__get__(  # type: ignore[method-assign]
        service,
        DiscordBotService,
    )

    interaction = _interaction(name="newt", options=[])
    interaction["id"] = "inter-1"
    interaction["token"] = "token-1"
    message_payload = {
        "id": "m-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "content": "please continue",
        "author": {"id": "user-1", "bot": False},
        "attachments": [],
    }

    try:
        await service._on_dispatch("INTERACTION_CREATE", interaction)
        await service._on_dispatch("MESSAGE_CREATE", message_payload)

        for _ in range(50):
            if any(
                "Queued (waiting for available worker...)"
                in item["payload"].get("content", "")
                for item in rest.channel_messages
            ):
                break
            await asyncio.sleep(0.01)

        assert observed == ["newt:start"]
        assert message_turn_started.is_set() is False
        queued_notice = next(
            (
                item["payload"]
                for item in rest.channel_messages
                if "Queued requests (1) behind /car newt"
                in item["payload"].get("content", "")
            ),
            None,
        )
        assert queued_notice is not None
        assert [
            button["custom_id"]
            for button in queued_notice["components"][0]["components"]
        ] == ["queue_cancel:m-1"]

        release_newt.set()

        await asyncio.wait_for(message_turn_started.wait(), timeout=5.0)

        assert observed == ["newt:start", "newt:end", "message:please continue"]
        assert any(
            "message reply" in item["payload"].get("content", "")
            for item in rest.channel_messages
        )
    finally:
        release_newt.set()
        await service._shutdown()
        await store.close()


@pytest.mark.anyio
async def test_dispatch_persists_runtime_state_only_after_ack(
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
    gateway = _FakeGateway([_interaction(name="newt", options=[])])
    service = DiscordBotService(
        _config(tmp_path, allow_user_ids=frozenset({"user-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    call_order: list[tuple[str, Any]] = []

    async def _tracked_ack(*_args: Any, **_kwargs: Any) -> bool:
        call_order.append(("ack", _kwargs.get("stage")))
        return True

    async def _tracked_persist(*_args: Any, **kwargs: Any) -> None:
        call_order.append(("persist", kwargs.get("scheduler_state")))

    def _tracked_submit(*_args: Any, **_kwargs: Any) -> None:
        call_order.append(("submit", None))

    service._acknowledge_runtime_envelope = _tracked_ack  # type: ignore[assignment]
    service._persist_runtime_interaction = _tracked_persist  # type: ignore[assignment]
    service._command_runner = SimpleNamespace(
        submit=_tracked_submit,
        shutdown=AsyncMock(),
    )

    try:
        await service.run_forever()
        assert call_order[:3] == [
            ("ack", "dispatch"),
            ("persist", "acknowledged"),
            ("submit", None),
        ]
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("subcommand", "expected_text"),
    [
        ("status", "hub manifest not configured"),
        ("start", "not bound"),
        ("restart", "not bound"),
    ],
)
async def test_public_flow_commands_keep_dispatch_preflight_errors_ephemeral(
    tmp_path: Path,
    subcommand: str,
    expected_text: str,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [_interaction_path(command_path=("car", "flow", subcommand), options=[])]
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
        assert payload["type"] == 5
        assert len(rest.followup_messages) == 1
        followup = rest.followup_messages[0]["payload"]
        assert followup["flags"] == 64
        assert expected_text in followup["content"].lower()
    finally:
        await store.close()
