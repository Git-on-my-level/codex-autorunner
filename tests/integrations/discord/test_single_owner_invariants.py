"""Characterization tests that pin architectural invariants for the Discord
interaction runtime v2, message-turn routing, and recovery behavior.

These tests exist as a guardrail for the 1700 Discord tech-debt band.  They
verify that the current seams and ownership boundaries remain in place even as
later tickets extract, move, or tighten ownership.

Invariants covered:

1. ``_on_dispatch`` routes ``INTERACTION_CREATE`` through the full runtime-v2
   admission path: ingress -> envelope -> ack -> persist -> register -> submit.
2. Rejected interactions (duplicate, normalization failure, unauthorized) never
   reach the scheduler.
3. ``MESSAGE_CREATE`` dispatches through ``_command_runner.submit_event()``.
4. Recovery marks records ``delivery_expired`` when no durable ack exists.
5. Recovery marks records ``abandoned`` when runtime envelope material is
   missing.
6. Unbound channel messages do not produce spurious orchestration routing.
7. The scheduler submission carries the resource keys and conversation id from
   the runtime-v2 envelope.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from codex_autorunner.core.orchestration import (
    SQLiteChatOperationLedger,
    initialize_orchestration_sqlite,
)
from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationEvaluationResult,
)
from codex_autorunner.integrations.discord.ingress import (
    CommandSpec,
    IngressContext,
    IngressResult,
    IngressTiming,
    InteractionKind,
    RuntimeInteractionEnvelope,
)
from codex_autorunner.integrations.discord.response_helpers import DiscordResponder
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


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


def _slash_payload(
    *,
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
    channel_id: str = "chan-1",
    guild_id: str = "guild-1",
    command_name: str = "car",
    subcommand_name: str = "status",
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "token": interaction_token,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": command_name,
            "options": [{"type": 1, "name": subcommand_name, "options": []}],
        },
    }


def _config(root: Path) -> Any:
    from codex_autorunner.integrations.discord.config import (
        DiscordBotConfig,
        DiscordBotDispatchConfig,
        DiscordCommandRegistration,
    )

    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({"guild-1"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_user_ids=frozenset({"user-1"}),
        command_registration=DiscordCommandRegistration(
            enabled=False, scope="guild", guild_ids=("guild-1",)
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=True,
        dispatch=DiscordBotDispatchConfig(ack_budget_ms=10_000),
    )


class _ChaosRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_responses: list[dict[str, Any]] = []

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
                "payload": dict(payload),
            }
        )

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
                "payload": dict(payload),
            }
        )
        return {"id": f"followup-{len(self.followup_messages)}"}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": "@original"}


def _build_recovery_service(
    *,
    store: DiscordStateStore,
    rest: _ChaosRest,
    operation_store: SQLiteChatOperationLedger,
) -> DiscordBotService:
    from codex_autorunner.integrations.discord.command_runner import (
        CommandRunner,
        RunnerConfig,
    )

    service = DiscordBotService.__new__(DiscordBotService)
    service._store = store
    service._chat_operation_store = operation_store
    service._rest = rest
    service._config = SimpleNamespace(
        application_id="app-1",
        max_message_length=2000,
        root=Path("."),
    )
    service._logger = logging.getLogger("test.discord.invariant.recovery")
    service._handle_car_command = AsyncMock()
    service._handle_pma_command = AsyncMock()
    service._handle_command_autocomplete = AsyncMock()
    service._handle_ticket_modal_submit = AsyncMock()
    service._respond_ephemeral = AsyncMock()
    service._send_or_respond_ephemeral = AsyncMock()
    service._evaluate_interaction_collaboration_policy = lambda **_kwargs: (
        CollaborationEvaluationResult(
            outcome="active_destination",
            allowed=True,
            command_allowed=True,
            should_start_turn=True,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=True,
            destination_mode="active",
            plain_text_trigger="always",
            reason="allowed",
        )
    )
    service._log_collaboration_policy_result = lambda **_kwargs: None
    service._responder = DiscordResponder(
        rest=rest,
        config=service._config,
        logger=service._logger,
        hydrate_ack_mode=service._load_interaction_ack_mode,
        record_ack=service._record_interaction_ack,
        record_delivery=service._record_interaction_delivery,
        record_delivery_cursor=service._record_interaction_delivery_cursor,
    )
    service._command_runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )
    return service


@pytest.mark.anyio
async def test_on_dispatch_routes_interaction_through_full_admission_path(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.dispatch_flow")
        service._config = _config(tmp_path)
        service._store = store
        service._hub_client = None
        service._ingress = SimpleNamespace()
        service._command_runner = SimpleNamespace(
            submit=Mock(), skip_submission_order=Mock()
        )
        service._persist_runtime_interaction = AsyncMock()
        service._register_interaction_ingress = AsyncMock(return_value=False)
        service._release_interaction_ingress = AsyncMock()
        service._register_chat_operation_received = AsyncMock()
        service._interaction_telemetry_fields = lambda *a, **kw: {}
        service._initial_ack_budget_seconds = lambda: 2.5
        service._respond_ephemeral = AsyncMock()
        service._get_interaction_session = lambda _token: None
        service._chat_adapter = SimpleNamespace()
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        import time as _time

        ctx = _make_ctx(interaction_id="inv-flow-1", interaction_token="tok-flow-1")
        ctx.timing = IngressTiming(ingress_started_at=_time.monotonic())
        envelope = RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id="conversation:discord:chan-1:guild-1",
            resource_keys=("conversation:discord:chan-1:guild-1",),
            dispatch_ack_policy="immediate",
        )

        service._ingress.process_raw_payload = AsyncMock(
            return_value=IngressResult(accepted=True, context=ctx)
        )
        service._ingress.finalize_success = Mock()
        service._build_runtime_interaction_envelope = AsyncMock(return_value=envelope)
        service._acknowledge_runtime_envelope = AsyncMock(return_value=True)

        payload = _slash_payload(
            interaction_id="inv-flow-1", interaction_token="tok-flow-1"
        )

        await service._on_dispatch("INTERACTION_CREATE", payload)

        service._ingress.process_raw_payload.assert_awaited_once_with(payload)
        service._build_runtime_interaction_envelope.assert_awaited_once_with(ctx)
        service._acknowledge_runtime_envelope.assert_awaited_once_with(
            envelope, stage="dispatch"
        )
        service._register_interaction_ingress.assert_awaited_once_with(ctx)
        service._persist_runtime_interaction.assert_awaited_once()
        call_kwargs = service._persist_runtime_interaction.call_args
        assert call_kwargs.kwargs["scheduler_state"] == "acknowledged"
        service._command_runner.submit.assert_called_once()
        submit_call = service._command_runner.submit.call_args
        assert submit_call.kwargs["resource_keys"] == envelope.resource_keys
        assert submit_call.kwargs["conversation_id"] == envelope.conversation_id
        service._ingress.finalize_success.assert_called_once_with(ctx)
        service._release_interaction_ingress.assert_awaited_once_with("inv-flow-1")
    finally:
        await store.close()


@pytest.mark.anyio
async def test_on_dispatch_rejected_duplicate_never_reaches_scheduler(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.dup_rejected")
        service._config = _config(tmp_path)
        service._store = store
        service._ingress = SimpleNamespace()
        service._command_runner = SimpleNamespace(
            submit=Mock(), skip_submission_order=Mock()
        )
        service._persist_runtime_interaction = AsyncMock()
        service._register_interaction_ingress = AsyncMock()
        service._release_interaction_ingress = AsyncMock()
        service._interaction_telemetry_fields = lambda *a, **kw: {}
        service._respond_ephemeral = AsyncMock()
        service._chat_adapter = SimpleNamespace()
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        ctx = _make_ctx(interaction_id="inv-dup-1", interaction_token="tok-dup-1")
        service._ingress.process_raw_payload = AsyncMock(
            return_value=IngressResult(
                accepted=False,
                context=ctx,
                rejection_reason="duplicate_interaction",
            )
        )

        payload = _slash_payload(
            interaction_id="inv-dup-1", interaction_token="tok-dup-1"
        )

        await service._on_dispatch("INTERACTION_CREATE", payload)

        service._command_runner.submit.assert_not_called()
        service._persist_runtime_interaction.assert_not_awaited()
        service._register_interaction_ingress.assert_not_awaited()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_on_dispatch_rejected_normalization_sends_error_and_skips_scheduler(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.norm_failed")
        service._config = _config(tmp_path)
        service._store = store
        service._ingress = SimpleNamespace()
        service._command_runner = SimpleNamespace(
            submit=Mock(), skip_submission_order=Mock()
        )
        service._persist_runtime_interaction = AsyncMock()
        service._register_interaction_ingress = AsyncMock()
        service._release_interaction_ingress = AsyncMock()
        service._interaction_telemetry_fields = lambda *a, **kw: {}
        service._respond_ephemeral = AsyncMock()
        service._chat_adapter = SimpleNamespace()
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        service._ingress.process_raw_payload = AsyncMock(
            return_value=IngressResult(
                accepted=False,
                context=None,
                rejection_reason="normalization_failed",
            )
        )

        payload = _slash_payload()

        await service._on_dispatch("INTERACTION_CREATE", payload)

        service._respond_ephemeral.assert_awaited_once()
        error_text = service._respond_ephemeral.call_args[0][2]
        assert "parse" in error_text.lower() or "retry" in error_text.lower()
        service._command_runner.submit.assert_not_called()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_on_dispatch_message_create_routes_through_command_runner_submit_event(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.msg_create")
        service._config = _config(tmp_path)
        service._store = store
        service._command_runner = SimpleNamespace(submit_event=Mock())
        service._channel_name_cache: dict[str, Any] = {}
        service._guild_name_cache: dict[str, Any] = {}
        service._coerce_id = lambda v: v
        service._first_non_empty_text = lambda *args: None
        service._nested_text = lambda p, *k: None
        service._resolve_channel_name = AsyncMock(return_value=None)
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        parsed_event = SimpleNamespace(kind="message_create")
        service._chat_adapter = SimpleNamespace(
            parse_message_event=Mock(return_value=parsed_event)
        )

        payload = {
            "id": "msg-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "author": {"id": "user-1"},
            "content": "hello",
        }

        await service._on_dispatch("MESSAGE_CREATE", payload)

        service._chat_adapter.parse_message_event.assert_called_once_with(payload)
        service._command_runner.submit_event.assert_called_once_with(parsed_event)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_on_dispatch_message_create_with_null_event_does_not_submit(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.msg_null")
        service._config = _config(tmp_path)
        service._store = store
        service._command_runner = SimpleNamespace(submit_event=Mock())
        service._channel_name_cache = {}
        service._guild_name_cache = {}
        service._coerce_id = lambda v: v
        service._first_non_empty_text = lambda *args: None
        service._nested_text = lambda p, *k: None
        service._resolve_channel_name = AsyncMock(return_value=None)
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        service._chat_adapter = SimpleNamespace(
            parse_message_event=Mock(return_value=None)
        )

        payload = {
            "id": "msg-null",
            "channel_id": "channel-1",
            "author": {"id": "bot-1", "bot": True},
            "content": "bot message",
        }

        await service._on_dispatch("MESSAGE_CREATE", payload)

        service._command_runner.submit_event.assert_not_called()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_recovery_marks_delivery_expired_when_no_durable_ack_exists(
    tmp_path: Path,
) -> None:
    initialize_orchestration_sqlite(tmp_path, durable=False)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    operation_store = SQLiteChatOperationLedger(tmp_path, durable=False)
    rest = _ChaosRest()
    await store.initialize()
    try:
        service = _build_recovery_service(
            store=store,
            rest=rest,
            operation_store=operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-no-ack-1",
            interaction_token="token-no-ack-1",
        )
        payload = _slash_payload(
            interaction_id="recover-no-ack-1",
            interaction_token="token-no-ack-1",
        )
        await store.register_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            interaction_kind=ctx.kind.value,
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            metadata_json=service._interaction_ledger_metadata(ctx),
        )
        envelope = RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id="conversation:discord:chan-1:guild-1",
            resource_keys=("conversation:discord:chan-1:guild-1",),
            dispatch_ack_policy="defer_ephemeral",
        )
        await service._persist_runtime_interaction(
            envelope,
            payload,
            scheduler_state="received",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        record = await store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "delivery_expired"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_recovery_marks_abandoned_when_payload_json_is_missing(
    tmp_path: Path,
) -> None:
    initialize_orchestration_sqlite(tmp_path, durable=False)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    operation_store = SQLiteChatOperationLedger(tmp_path, durable=False)
    rest = _ChaosRest()
    await store.initialize()
    try:
        service = _build_recovery_service(
            store=store,
            rest=rest,
            operation_store=operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-no-payload-1",
            interaction_token="token-no-payload-1",
        )
        payload = _slash_payload(
            interaction_id="recover-no-payload-1",
            interaction_token="token-no-payload-1",
        )
        await store.register_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            interaction_kind=ctx.kind.value,
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            metadata_json=service._interaction_ledger_metadata(ctx),
        )
        envelope = RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id="conversation:discord:chan-1:guild-1",
            resource_keys=("conversation:discord:chan-1:guild-1",),
            dispatch_ack_policy="defer_ephemeral",
        )
        await service._persist_runtime_interaction(
            envelope,
            payload,
            scheduler_state="acknowledged",
        )
        await store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="running",
        )

        def _null_payload() -> None:
            conn = store._connection_sync()
            conn.execute(
                "UPDATE interaction_ledger SET payload_json = NULL WHERE interaction_id = ?",
                (ctx.interaction_id,),
            )
            conn.commit()

        await store._run(_null_payload)

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        record = await store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "abandoned"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_unbound_channel_message_does_not_produce_orchestration_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import codex_autorunner.integrations.discord.message_turns as discord_message_turns
    from codex_autorunner.integrations.chat.dispatcher import build_dispatch_context
    from codex_autorunner.integrations.chat.models import (
        ChatMessageEvent,
        ChatMessageRef,
        ChatThreadRef,
    )

    class _StoreStub:
        async def get_binding(self, *, channel_id: str) -> dict[str, object] | None:
            return None

    class _ServiceStub:
        def __init__(self) -> None:
            self._store = _StoreStub()
            self._logger = logging.getLogger("test")

    ingress_called = False

    class _IngressStub:
        async def submit_message(self, *args: object, **kwargs: object) -> None:
            nonlocal ingress_called
            ingress_called = True
            raise AssertionError(
                "unbound channel should not reach orchestration ingress"
            )

    monkeypatch.setattr(
        discord_message_turns,
        "build_surface_orchestration_ingress",
        lambda **_: _IngressStub(),
    )

    thread = ChatThreadRef(
        platform="discord", chat_id="unbound-channel", thread_id=None
    )
    event = ChatMessageEvent(
        update_id="update-unbound",
        thread=thread,
        message=ChatMessageRef(thread=thread, message_id="msg-unbound"),
        from_user_id="user-1",
        text="hello from unbound",
    )
    context = build_dispatch_context(event)

    log_calls: list[dict[str, Any]] = []

    def log_fn(*args: Any, **kwargs: Any) -> None:
        log_calls.append({"args": args, "kwargs": kwargs})

    await discord_message_turns.handle_message_event(
        _ServiceStub(),
        event,
        context,
        channel_id="unbound-channel",
        text="hello from unbound",
        has_attachments=False,
        policy_result=None,
        log_event_fn=log_fn,
        build_ticket_flow_controller_fn=lambda *_args, **_kwargs: None,
        ensure_worker_fn=lambda *_args, **_kwargs: None,
    )

    assert ingress_called is False


@pytest.mark.anyio
async def test_admitted_interaction_uses_runtime_v2_envelope_for_scheduler_submission(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    try:
        service = DiscordBotService.__new__(DiscordBotService)
        service._logger = logging.getLogger("test.invariant.envelope_submit")
        service._config = _config(tmp_path)
        service._store = store
        service._hub_client = None
        service._ingress = SimpleNamespace()
        service._command_runner = SimpleNamespace(
            submit=Mock(), skip_submission_order=Mock()
        )
        service._persist_runtime_interaction = AsyncMock()
        service._register_interaction_ingress = AsyncMock(return_value=False)
        service._release_interaction_ingress = AsyncMock()
        service._register_chat_operation_received = AsyncMock()
        service._interaction_telemetry_fields = lambda *a, **kw: {}
        service._initial_ack_budget_seconds = lambda: 2.5
        service._respond_ephemeral = AsyncMock()
        service._get_interaction_session = lambda _token: None
        service._chat_adapter = SimpleNamespace()
        service._spawn_task = lambda coro: asyncio.create_task(coro)
        service._record_channel_directory_seen_from_message_payload = AsyncMock()

        import time as _time

        ctx = _make_ctx(
            interaction_id="inv-envelope-1",
            interaction_token="tok-envelope-1",
            channel_id="chan-1",
            guild_id="guild-1",
        )
        ctx.timing = IngressTiming(ingress_started_at=_time.monotonic())
        envelope = RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id="conversation:discord:chan-1:guild-1",
            resource_keys=(
                "conversation:discord:chan-1:guild-1",
                "workspace:/tmp/ws-x",
            ),
            dispatch_ack_policy="defer_ephemeral",
            queue_wait_ack_policy="defer_ephemeral",
        )

        service._ingress.process_raw_payload = AsyncMock(
            return_value=IngressResult(accepted=True, context=ctx)
        )
        service._ingress.finalize_success = Mock()
        service._build_runtime_interaction_envelope = AsyncMock(return_value=envelope)
        service._acknowledge_runtime_envelope = AsyncMock(return_value=True)

        payload = _slash_payload(
            interaction_id="inv-envelope-1",
            interaction_token="tok-envelope-1",
        )

        await service._on_dispatch("INTERACTION_CREATE", payload)

        submit_call = service._command_runner.submit.call_args
        assert submit_call.kwargs["resource_keys"] == (
            "conversation:discord:chan-1:guild-1",
            "workspace:/tmp/ws-x",
        )
        assert (
            submit_call.kwargs["conversation_id"]
            == "conversation:discord:chan-1:guild-1"
        )
        assert submit_call.kwargs["queue_wait_ack_policy"] == "defer_ephemeral"
    finally:
        await store.close()
