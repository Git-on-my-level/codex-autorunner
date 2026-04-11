from __future__ import annotations

import json
import logging
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.integrations.discord.effects import (
    DiscordEffectDeliveryError,
    DiscordResponseEffect,
)
from codex_autorunner.integrations.discord.errors import DiscordTransientError
from codex_autorunner.integrations.discord.ingress import (
    CommandSpec,
    IngressContext,
    IngressTiming,
    InteractionKind,
)
from codex_autorunner.integrations.discord.interaction_dispatch import (
    execute_ingressed_interaction,
)


def _ctx(
    *,
    kind: InteractionKind = InteractionKind.SLASH_COMMAND,
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
    channel_id: str = "chan-1",
    custom_id: Optional[str] = None,
    values: Optional[list[str]] = None,
    modal_values: Optional[dict[str, Any]] = None,
    focused_name: Optional[str] = None,
    focused_value: Optional[str] = None,
    command_path: tuple[str, ...] = ("car", "status"),
    deferred: bool = True,
) -> IngressContext:
    command_spec = (
        CommandSpec(
            path=command_path,
            options={},
            ack_policy="defer_ephemeral",
            ack_timing="dispatch",
            requires_workspace=False,
        )
        if kind in (InteractionKind.SLASH_COMMAND, InteractionKind.AUTOCOMPLETE)
        else None
    )
    return IngressContext(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        channel_id=channel_id,
        guild_id="guild-1",
        user_id="user-1",
        kind=kind,
        deferred=deferred,
        command_spec=command_spec,
        custom_id=custom_id,
        values=values,
        modal_values=modal_values,
        focused_name=focused_name,
        focused_value=focused_value,
        timing=IngressTiming(),
    )


class _FakeService:
    def __init__(self) -> None:
        self._logger = logging.getLogger("test.exec_ingressed")
        self._handle_car_command = AsyncMock()
        self._handle_pma_command = AsyncMock()
        self._handle_command_autocomplete = AsyncMock()
        self._handle_ticket_modal_submit = AsyncMock()
        self._handle_ticket_filter_component = AsyncMock()
        self._handle_ticket_select_component = AsyncMock()
        self._handle_approval_component = AsyncMock()
        self.respond_ephemeral = AsyncMock()
        self.respond_autocomplete = AsyncMock()


@pytest.mark.anyio
async def test_slash_command_routes_to_car_handler() -> None:
    service = _FakeService()
    ctx = _ctx(command_path=("car", "bind"))
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service._handle_car_command.assert_awaited_once()
    call_kwargs = service._handle_car_command.call_args[1]
    assert call_kwargs["command_path"] == ("car", "bind")


@pytest.mark.anyio
async def test_slash_command_routes_to_pma_handler() -> None:
    service = _FakeService()
    ctx = _ctx(command_path=("pma", "status"))
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service._handle_pma_command.assert_awaited_once()


@pytest.mark.anyio
async def test_autocomplete_routes_to_autocomplete_handler() -> None:
    service = _FakeService()
    ctx = _ctx(
        kind=InteractionKind.AUTOCOMPLETE,
        command_path=("car", "bind"),
        focused_name="workspace",
        focused_value="codex",
    )
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service._handle_command_autocomplete.assert_awaited_once()
    call_kwargs = service._handle_command_autocomplete.call_args[1]
    assert call_kwargs["focused_name"] == "workspace"
    assert call_kwargs["focused_value"] == "codex"


@pytest.mark.anyio
async def test_modal_submit_routes_to_modal_handler() -> None:
    service = _FakeService()
    ctx = _ctx(
        kind=InteractionKind.MODAL_SUBMIT,
        custom_id="tickets_modal:abc",
        modal_values={"ticket_body": "test body"},
    )
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service._handle_ticket_modal_submit.assert_awaited_once()
    call_kwargs = service._handle_ticket_modal_submit.call_args[1]
    assert call_kwargs["custom_id"] == "tickets_modal:abc"
    assert call_kwargs["values"] == {"ticket_body": "test body"}


@pytest.mark.anyio
async def test_modal_submit_transient_error_responds_ephemeral() -> None:
    service = _FakeService()
    service._handle_ticket_modal_submit.side_effect = DiscordTransientError(
        "expired",
        user_message="This ticket modal has expired. Re-open it and try again.",
    )
    ctx = _ctx(
        kind=InteractionKind.MODAL_SUBMIT,
        custom_id="tickets_modal:abc",
        modal_values={"ticket_body": "test body"},
    )

    await execute_ingressed_interaction(service, ctx, {})

    service.respond_ephemeral.assert_awaited_once()
    call_args = service.respond_ephemeral.call_args
    assert "expired" in call_args[0][2].lower()


@pytest.mark.anyio
async def test_autocomplete_transient_error_returns_empty_choices() -> None:
    service = _FakeService()
    service._handle_command_autocomplete.side_effect = DiscordTransientError(
        "autocomplete failed",
        user_message="busy",
    )
    ctx = _ctx(
        kind=InteractionKind.AUTOCOMPLETE,
        command_path=("car", "bind"),
        focused_name="workspace",
        focused_value="codex",
    )

    await execute_ingressed_interaction(service, ctx, {})

    service.respond_autocomplete.assert_awaited_once_with(
        "inter-1",
        "token-1",
        choices=[],
    )


@pytest.mark.anyio
async def test_autocomplete_delivery_error_returns_empty_choices() -> None:
    service = _FakeService()
    service._handle_command_autocomplete.side_effect = DiscordEffectDeliveryError(
        effect=DiscordResponseEffect(text="busy", ephemeral=True),
        interaction_id="inter-1",
        interaction_token="token-1",
        delivery_status="ack_failed",
    )
    ctx = _ctx(
        kind=InteractionKind.AUTOCOMPLETE,
        command_path=("car", "bind"),
        focused_name="workspace",
        focused_value="codex",
    )

    await execute_ingressed_interaction(service, ctx, {})

    service.respond_autocomplete.assert_awaited_once_with(
        "inter-1",
        "token-1",
        choices=[],
    )


@pytest.mark.anyio
async def test_component_with_custom_id_routes_to_component_handler() -> None:
    service = _FakeService()
    ctx = _ctx(
        kind=InteractionKind.COMPONENT,
        custom_id="tickets_filter_select",
        values=["open"],
        deferred=False,
    )
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service._handle_ticket_filter_component.assert_awaited_once()
    call_kwargs = service._handle_ticket_filter_component.call_args[1]
    assert call_kwargs["values"] == ["open"]


@pytest.mark.anyio
async def test_component_without_custom_id_responds_error() -> None:
    service = _FakeService()
    ctx = _ctx(
        kind=InteractionKind.COMPONENT,
        custom_id=None,
        deferred=False,
    )
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service.respond_ephemeral.assert_awaited_once()
    call_args = service.respond_ephemeral.call_args
    assert "could not identify" in call_args[0][2].lower()


@pytest.mark.anyio
async def test_slash_command_without_command_spec_responds_error() -> None:
    service = _FakeService()
    ctx = _ctx(command_path=())
    ctx.command_spec = CommandSpec(
        path=(),
        options={},
        ack_policy=None,
        ack_timing="dispatch",
        requires_workspace=False,
    )
    payload: dict[str, Any] = {}

    await execute_ingressed_interaction(service, ctx, payload)

    service.respond_ephemeral.assert_awaited()
    call_args = service.respond_ephemeral.call_args
    assert "could not parse" in call_args[0][2].lower()


@pytest.mark.anyio
async def test_callback_errors_are_logged_separately_from_handler_errors() -> None:
    service = _FakeService()
    callback_events: list[dict[str, Any]] = []
    original_log = service._logger.log

    def capture_log(level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        try:
            parsed = json.loads(msg)
        except (TypeError, json.JSONDecodeError):
            pass
        else:
            if parsed.get("event") == "discord.runner.callback_error":
                callback_events.append(parsed)
        original_log(level, msg, *args, **kwargs)

    service._logger.log = capture_log  # type: ignore[assignment]
    service._handle_car_command.side_effect = DiscordEffectDeliveryError(
        effect=DiscordResponseEffect(text="boom", ephemeral=True),
        interaction_id="inter-1",
        interaction_token="token-1",
        delivery_status="followup_failed",
        delivery_error="callback failed",
    )
    ctx = _ctx(command_path=("car", "status"))

    await execute_ingressed_interaction(service, ctx, {})

    assert callback_events
    assert callback_events[0]["interaction_id"] == "inter-1"
    service.respond_ephemeral.assert_not_awaited()
