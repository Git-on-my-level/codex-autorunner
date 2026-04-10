from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.integrations.chat.dispatcher import conversation_id_for
from codex_autorunner.integrations.discord.command_runner import (
    CommandRunner,
    RunnerConfig,
)
from codex_autorunner.integrations.discord.ingress import (
    CommandSpec,
    IngressContext,
    IngressTiming,
    InteractionKind,
)


def _make_ctx(
    *,
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
    channel_id: str = "chan-1",
    kind: InteractionKind = InteractionKind.SLASH_COMMAND,
    deferred: bool = True,
    command_path: tuple[str, ...] = ("car", "status"),
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
    custom_id: Optional[str] = None,
    values: Optional[list[str]] = None,
    modal_values: Optional[dict[str, Any]] = None,
    focused_name: Optional[str] = None,
    focused_value: Optional[str] = None,
    message_id: Optional[str] = None,
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
        guild_id=guild_id,
        user_id=user_id,
        kind=kind,
        deferred=deferred,
        command_spec=command_spec,
        custom_id=custom_id,
        values=values,
        modal_values=modal_values,
        focused_name=focused_name,
        focused_value=focused_value,
        message_id=message_id,
        timing=IngressTiming(),
    )


class _FakeService:
    def __init__(self) -> None:
        self._logger = logging.getLogger("test.runner")
        self._handle_car_command = AsyncMock()
        self._handle_pma_command = AsyncMock()
        self._handle_command_autocomplete = AsyncMock()
        self._handle_ticket_modal_submit = AsyncMock()
        self._respond_ephemeral = AsyncMock()
        self._send_or_respond_ephemeral = AsyncMock()


def _slash_payload(
    *,
    command_name: str = "car",
    subcommand_name: str = "status",
) -> dict[str, Any]:
    return {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": command_name,
            "options": [{"type": 1, "name": subcommand_name, "options": []}],
        },
    }


@pytest.mark.anyio
async def test_submit_runs_handler_in_background() -> None:
    service = _FakeService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_car_command.assert_awaited_once()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_slow_handler_does_not_block_new_interaction() -> None:
    service = _FakeService()
    slow_done = asyncio.Event()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await slow_done.wait()

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    ctx_slow = _make_ctx(interaction_id="slow-1", interaction_token="slow-tok-1")
    payload_slow = _slash_payload()

    runner.submit(ctx_slow, payload_slow)
    await asyncio.sleep(0.02)
    assert runner.active_task_count == 1

    ingress_ack_time = asyncio.get_event_loop().time()

    ctx_fast = _make_ctx(
        interaction_id="fast-1",
        interaction_token="fast-tok-1",
        command_path=("car", "status"),
    )
    payload_fast = {**_slash_payload(), "id": "fast-1", "token": "fast-tok-1"}
    runner.submit(ctx_fast, payload_fast)

    fast_elapsed = asyncio.get_event_loop().time() - ingress_ack_time
    assert fast_elapsed < 1.0

    slow_done.set()
    await asyncio.sleep(0.05)
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_timeout_enforcement() -> None:
    service = _FakeService()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.1, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.sleep(0.3)

    service._send_or_respond_ephemeral.assert_awaited()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_timeout_sends_user_message() -> None:
    service = _FakeService()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.sleep(0.3)

    service._send_or_respond_ephemeral.assert_awaited_once()
    call_kwargs = service._send_or_respond_ephemeral.call_args[1]
    assert "timed out" in call_kwargs["text"].lower()
    assert call_kwargs["interaction_id"] == "inter-1"


@pytest.mark.anyio
async def test_shutdown_waits_for_timeout_followup_tasks() -> None:
    service = _FakeService()
    followup_started = asyncio.Event()
    followup_cancelled = asyncio.Event()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    async def blocked_followup(**kwargs: Any) -> None:
        followup_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            followup_cancelled.set()
            raise

    service._handle_car_command.side_effect = slow_handler
    service._send_or_respond_ephemeral.side_effect = blocked_followup

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    shutdown_task = asyncio.create_task(runner.shutdown(grace_seconds=0.2))

    await asyncio.wait_for(followup_started.wait(), timeout=1.0)
    await shutdown_task

    assert followup_cancelled.is_set()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_none_timeout_allows_long_running_handler() -> None:
    service = _FakeService()
    finished = asyncio.Event()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.2)
        finished.set()

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    for _ in range(20):
        if runner.active_task_count == 0:
            break
        await asyncio.sleep(0.01)

    service._send_or_respond_ephemeral.assert_not_awaited()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_handler_error_sends_error_followup() -> None:
    service = _FakeService()

    async def failing_handler(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    service._handle_car_command.side_effect = failing_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._respond_ephemeral.assert_awaited()
    call_args = service._respond_ephemeral.call_args
    assert "unexpected error" in call_args[0][2].lower()


@pytest.mark.anyio
async def test_shutdown_cancels_running_tasks() -> None:
    service = _FakeService()

    async def long_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    service._handle_car_command.side_effect = long_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=60.0),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await asyncio.sleep(0.02)
    assert runner.active_task_count == 1

    await runner.shutdown()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_shutdown_drains_fast_tasks() -> None:
    service = _FakeService()

    async def fast_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.01)

    service._handle_car_command.side_effect = fast_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    ctx = _make_ctx()
    payload = _slash_payload()

    runner.submit(ctx, payload)
    await runner.shutdown(grace_seconds=1.0)
    assert runner.active_task_count == 0
    service._handle_car_command.assert_awaited_once()


@pytest.mark.anyio
async def test_shutdown_is_idempotent() -> None:
    service = _FakeService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    await runner.shutdown()
    await runner.shutdown()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_autocomplete_interaction_routing() -> None:
    service = _FakeService()
    ctx = _make_ctx(
        kind=InteractionKind.AUTOCOMPLETE,
        command_path=("car", "bind"),
        focused_name="workspace",
        focused_value="codex",
    )
    payload: dict[str, Any] = {
        "id": "inter-4",
        "token": "token-4",
        "channel_id": "chan-4",
        "type": 4,
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": "bind",
                    "options": [
                        {
                            "type": 3,
                            "name": "workspace",
                            "value": "codex",
                            "focused": True,
                        }
                    ],
                }
            ],
        },
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_command_autocomplete.assert_awaited_once()
    call_args = service._handle_command_autocomplete.call_args
    assert call_args[1]["focused_name"] == "workspace"
    assert call_args[1]["focused_value"] == "codex"


@pytest.mark.anyio
async def test_modal_submit_routing() -> None:
    service = _FakeService()
    ctx = _make_ctx(
        kind=InteractionKind.MODAL_SUBMIT,
        custom_id="tickets_modal:abc",
        modal_values={"ticket_body": "test body"},
    )
    payload: dict[str, Any] = {
        "id": "inter-3",
        "token": "token-3",
        "channel_id": "chan-3",
        "type": 5,
        "data": {
            "custom_id": "tickets_modal:abc",
            "components": [],
        },
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_ticket_modal_submit.assert_awaited_once()
    call_args = service._handle_ticket_modal_submit.call_args
    assert call_args[1]["custom_id"] == "tickets_modal:abc"


@pytest.mark.anyio
async def test_pma_command_routing() -> None:
    service = _FakeService()
    ctx = _make_ctx(command_path=("pma", "status"))
    payload = _slash_payload(command_name="pma", subcommand_name="status")

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_pma_command.assert_awaited_once()


@pytest.mark.anyio
async def test_multiple_submits_concurrent() -> None:
    service = _FakeService()
    handler_events: list[asyncio.Event] = []
    handler_count = 0

    async def counting_handler(*args: Any, **kwargs: Any) -> None:
        nonlocal handler_count
        handler_count += 1
        evt = asyncio.Event()
        handler_events.append(evt)
        await evt.wait()

    service._handle_car_command.side_effect = counting_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    for i in range(5):
        ctx = _make_ctx(
            interaction_id=f"inter-{i}",
            interaction_token=f"token-{i}",
        )
        runner.submit(ctx, _slash_payload())

    await asyncio.sleep(0.05)
    assert runner.active_task_count == 5
    assert handler_count == 5

    for evt in handler_events:
        evt.set()
    await asyncio.sleep(0.05)
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_submit_event_delegates_to_dispatch_chat_event() -> None:
    service = _FakeService()
    dispatch_event = AsyncMock()
    service._dispatch_chat_event = dispatch_event

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    fake_event = object()
    runner.submit_event(fake_event)
    await runner.shutdown(grace_seconds=2.0)

    dispatch_event.assert_awaited_once_with(fake_event)


@pytest.mark.anyio
async def test_submit_event_preserves_arrival_order() -> None:
    service = _FakeService()
    dispatched_order: list[str] = []

    async def track_dispatch(event: Any) -> None:
        dispatched_order.append(event["label"])
        if event.get("block"):
            await asyncio.sleep(0.05)

    service._dispatch_chat_event = track_dispatch

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    runner.submit_event({"label": "first", "block": True})
    runner.submit_event({"label": "second", "block": False})
    runner.submit_event({"label": "third", "block": False})

    await runner.shutdown(grace_seconds=5.0)
    assert dispatched_order == ["first", "second", "third"]


@pytest.mark.anyio
async def test_submit_serializes_fifo_within_conversation() -> None:
    service = _FakeService()
    started_ids: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def ordered_handler(*args: Any, **_kwargs: Any) -> None:
        interaction_id = str(args[0]) if args else ""
        started_ids.append(interaction_id)
        if interaction_id == "inter-1":
            first_started.set()
            await release_first.wait()

    service._handle_car_command.side_effect = ordered_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )

    ctx_first = _make_ctx(
        interaction_id="inter-1",
        interaction_token="token-1",
        channel_id="chan-1",
        guild_id="guild-1",
    )
    ctx_second = _make_ctx(
        interaction_id="inter-2",
        interaction_token="token-2",
        channel_id="chan-1",
        guild_id="guild-1",
    )
    conversation_id = conversation_id_for("discord", "chan-1", "guild-1")
    resource_keys = (f"conversation:{conversation_id}",)

    runner.submit(
        ctx_first,
        _slash_payload(),
        resource_keys=resource_keys,
        conversation_id=conversation_id,
    )
    runner.submit(
        ctx_second,
        _slash_payload(),
        resource_keys=resource_keys,
        conversation_id=conversation_id,
    )

    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert started_ids == ["inter-1"]

    release_first.set()
    await runner.shutdown(grace_seconds=5.0)
    assert started_ids == ["inter-1", "inter-2"]


@pytest.mark.anyio
async def test_submit_allows_other_conversations_to_run() -> None:
    service = _FakeService()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def blocking_handler(*args: Any, **_kwargs: Any) -> None:
        interaction_id = str(args[0]) if args else ""
        if interaction_id == "inter-1":
            first_started.set()
            await release_first.wait()
            return
        if interaction_id == "inter-2":
            second_started.set()

    service._handle_car_command.side_effect = blocking_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )

    ctx_first = _make_ctx(
        interaction_id="inter-1",
        interaction_token="token-1",
        channel_id="chan-1",
        guild_id="guild-1",
    )
    ctx_second = _make_ctx(
        interaction_id="inter-2",
        interaction_token="token-2",
        channel_id="chan-2",
        guild_id="guild-1",
    )
    conversation_id_first = conversation_id_for("discord", "chan-1", "guild-1")
    conversation_id_second = conversation_id_for("discord", "chan-2", "guild-1")

    runner.submit(
        ctx_first,
        _slash_payload(),
        resource_keys=(f"conversation:{conversation_id_first}",),
        conversation_id=conversation_id_first,
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    runner.submit(
        ctx_second,
        _slash_payload(),
        resource_keys=(f"conversation:{conversation_id_second}",),
        conversation_id=conversation_id_second,
    )
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    release_first.set()
    await runner.shutdown(grace_seconds=5.0)


@pytest.mark.anyio
async def test_component_interaction_routes_through_handle_component() -> None:
    service = _FakeService()
    service._handle_ticket_filter_component = AsyncMock()
    service._require_bound_workspace = AsyncMock(return_value="/tmp/ws")
    service._handle_flow_status = AsyncMock()
    service._handle_approval_component = AsyncMock()

    ctx = _make_ctx(
        kind=InteractionKind.COMPONENT,
        custom_id="approval:abc",
        values=None,
        message_id="msg-1",
        deferred=False,
    )
    payload: dict[str, Any] = {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "type": 3,
        "data": {"custom_id": "approval:abc"},
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_approval_component.assert_awaited_once()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_component_select_with_values_routes_correctly() -> None:
    service = _FakeService()
    service._handle_ticket_filter_component = AsyncMock()

    ctx = _make_ctx(
        kind=InteractionKind.COMPONENT,
        custom_id="tickets_filter_select",
        values=["open"],
        deferred=False,
    )
    payload: dict[str, Any] = {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "type": 3,
        "data": {"custom_id": "tickets_filter_select", "values": ["open"]},
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._handle_ticket_filter_component.assert_awaited_once()
    call_kwargs = service._handle_ticket_filter_component.call_args
    assert call_kwargs[1]["values"] == ["open"]


@pytest.mark.anyio
async def test_component_without_custom_id_responds_error() -> None:
    service = _FakeService()

    ctx = _make_ctx(
        kind=InteractionKind.COMPONENT,
        custom_id=None,
        deferred=False,
    )
    payload: dict[str, Any] = {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "type": 3,
        "data": {},
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._respond_ephemeral.assert_awaited()
    call_args = service._respond_ephemeral.call_args
    assert "could not identify" in call_args[0][2].lower()


@pytest.mark.anyio
async def test_unknown_component_responds_unknown_message() -> None:
    service = _FakeService()

    ctx = _make_ctx(
        kind=InteractionKind.COMPONENT,
        custom_id="nonexistent_button",
        deferred=False,
    )
    payload: dict[str, Any] = {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "type": 3,
        "data": {"custom_id": "nonexistent_button"},
    }

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    service._respond_ephemeral.assert_awaited()
    call_args = service._respond_ephemeral.call_args
    assert "Unknown component" in call_args[0][2]
