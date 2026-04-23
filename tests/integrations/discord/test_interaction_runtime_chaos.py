from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.core.orchestration import (
    SQLiteChatOperationLedger,
    initialize_orchestration_sqlite,
)
from codex_autorunner.integrations.chat.collaboration_policy import (
    CollaborationEvaluationResult,
)
from codex_autorunner.integrations.chat.dispatcher import conversation_id_for
from codex_autorunner.integrations.discord.command_runner import (
    CommandRunner,
    RunnerConfig,
)
from codex_autorunner.integrations.discord.ingress import (
    CommandSpec,
    IngressContext,
    IngressTiming,
    InteractionIngress,
    InteractionKind,
    RuntimeInteractionEnvelope,
)
from codex_autorunner.integrations.discord.interaction_session import (
    InteractionSessionError,
)
from codex_autorunner.integrations.discord.response_helpers import DiscordResponder
from codex_autorunner.integrations.discord.service import (
    _INTERACTION_RECOVERY_METADATA_KEY,
    DiscordBotService,
)
from codex_autorunner.integrations.discord.state import DiscordStateStore

pytestmark = pytest.mark.slow


def _logged_events(
    caplog: pytest.LogCaptureFixture, logger_name: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != logger_name:
            continue
        message = record.getMessage()
        if not message.startswith("{"):
            continue
        events.append(json.loads(message))
    return events


def _make_ctx(
    *,
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
    channel_id: str = "chan-1",
    guild_id: Optional[str] = "guild-1",
    kind: InteractionKind = InteractionKind.SLASH_COMMAND,
    deferred: bool = True,
    command_path: tuple[str, ...] = ("car", "status"),
) -> IngressContext:
    command_spec = (
        CommandSpec(
            path=command_path,
            options={},
            ack_policy="defer_ephemeral",
            ack_timing="dispatch",
            requires_workspace=False,
        )
        if kind == InteractionKind.SLASH_COMMAND
        else None
    )
    return IngressContext(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id="user-1",
        kind=kind,
        deferred=deferred,
        command_spec=command_spec,
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


@dataclass(frozen=True)
class _FaultPlan:
    delay_seconds: float = 0.0
    exc: Optional[Exception] = None
    response: Optional[dict[str, Any]] = None


class _ChaosRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_responses: list[dict[str, Any]] = []
        self._faults: dict[str, list[_FaultPlan]] = {}

    def inject_fault(
        self,
        method_name: str,
        *,
        delay_seconds: float = 0.0,
        exc: Optional[Exception] = None,
        response: Optional[dict[str, Any]] = None,
    ) -> None:
        self._faults.setdefault(method_name, []).append(
            _FaultPlan(
                delay_seconds=delay_seconds,
                exc=exc,
                response=response,
            )
        )

    async def _apply_fault(self, method_name: str) -> Optional[dict[str, Any]]:
        queue = self._faults.get(method_name)
        if not queue:
            return None
        plan = queue.pop(0)
        if plan.delay_seconds > 0:
            await asyncio.sleep(plan.delay_seconds)
        if plan.exc is not None:
            raise plan.exc
        return plan.response

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
        await self._apply_fault("create_interaction_response")

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
        fault_response = await self._apply_fault("create_followup_message")
        if fault_response is not None:
            return fault_response
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
        fault_response = await self._apply_fault("edit_original_interaction_response")
        if fault_response is not None:
            return fault_response
        return {"id": "@original"}


class _ChaosHarness:
    def __init__(self, tmp_path: Path) -> None:
        initialize_orchestration_sqlite(tmp_path, durable=False)
        self.store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
        self.operation_store = SQLiteChatOperationLedger(tmp_path, durable=False)
        self.rest = _ChaosRest()
        self.config = SimpleNamespace(
            application_id="app-1",
            max_message_length=2000,
            root=tmp_path,
        )
        self.logger = logging.getLogger("test.discord.chaos")

    async def initialize(self) -> None:
        await self.store.initialize()

    async def close(self) -> None:
        await self.store.close()

    async def register_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        channel_id: str = "chan-1",
        guild_id: str = "guild-1",
        interaction_kind: str = "slash_command",
    ) -> None:
        await self.store.register_interaction(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            interaction_kind=interaction_kind,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id="user-1",
            metadata_json={"command_path": ["car", "status"]},
        )

    async def load_ack_mode(self, interaction_id: str) -> Optional[str]:
        record = await self.store.get_interaction(interaction_id)
        return record.ack_mode if record is not None else None

    async def record_ack(
        self,
        interaction_id: str,
        interaction_token: str,
        ack_mode: str,
        original_response_message_id: Optional[str],
    ) -> None:
        _ = interaction_token
        await self.store.mark_interaction_acknowledged(
            interaction_id,
            ack_mode=ack_mode,
            original_response_message_id=original_response_message_id,
        )

    async def record_delivery(
        self,
        interaction_id: str,
        delivery_status: str,
        delivery_error: Optional[str],
        original_response_message_id: Optional[str],
    ) -> None:
        await self.store.record_interaction_delivery(
            interaction_id,
            delivery_status=delivery_status,
            delivery_error=delivery_error,
            original_response_message_id=original_response_message_id,
        )

    def responder(self) -> DiscordResponder:
        return DiscordResponder(
            rest=self.rest,
            config=self.config,
            logger=self.logger,
            hydrate_ack_mode=self.load_ack_mode,
            record_ack=self.record_ack,
            record_delivery=self.record_delivery,
        )


class _IngressStoreService:
    def __init__(self, store: DiscordStateStore) -> None:
        self._store = store
        self._logger = logging.getLogger("test.discord.chaos.ingress")
        self.respond_ephemeral_calls: list[dict[str, str]] = []
        self.prepare_command_calls: list[dict[str, Any]] = []
        self._sessions: dict[str, _IngressSession] = {}

    def _ensure_interaction_session(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: Any = None,
    ) -> "_IngressSession":
        _ = interaction_id, kind
        session = self._sessions.get(interaction_token)
        if session is None:
            session = _IngressSession()
            self._sessions[interaction_token] = session
        return session

    async def _register_interaction_ingress(self, ctx: IngressContext) -> bool:
        registration = await self._store.register_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            interaction_kind=ctx.kind.value,
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            metadata_json={"command_path": list(ctx.command_spec.path)},
        )
        return not registration.inserted

    def _evaluate_interaction_collaboration_policy(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> CollaborationEvaluationResult:
        _ = channel_id, guild_id, user_id
        return CollaborationEvaluationResult(
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

    def _log_collaboration_policy_result(self, **kwargs: Any) -> None:
        _ = kwargs

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        self.respond_ephemeral_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "text": text,
            }
        )

    async def _prepare_command_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        command_path: tuple[str, ...],
        timing: str,
    ) -> bool:
        self.prepare_command_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "command_path": command_path,
                "timing": timing,
            }
        )
        return True

    @staticmethod
    def _normalize_discord_command_path(
        command_path: tuple[str, ...],
    ) -> tuple[str, ...]:
        return command_path


class _IngressSession:
    def has_initial_response(self) -> bool:
        return False


class _RunnerService:
    def __init__(self) -> None:
        self._logger = logging.getLogger("test.discord.chaos.runner")
        self._handle_car_command = AsyncMock()
        self._handle_pma_command = AsyncMock()
        self._handle_command_autocomplete = AsyncMock()
        self._handle_ticket_modal_submit = AsyncMock()
        self._respond_ephemeral = AsyncMock()
        self._send_or_respond_ephemeral = AsyncMock()
        self._dispatch_chat_event = AsyncMock()

    async def dispatch_chat_event(self, event: Any) -> None:
        await self._dispatch_chat_event(event)

    async def acknowledge_runtime_envelope(
        self,
        _envelope: Any,
        *,
        stage: str,
    ) -> bool:
        _ = stage
        return True

    async def mark_interaction_scheduler_state(
        self,
        _ctx: Any,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
    ) -> None:
        _ = scheduler_state, increment_attempt_count

    async def begin_interaction_execution(self, _ctx: Any) -> bool:
        return True

    async def begin_interaction_recovery_execution(self, _ctx: Any) -> bool:
        return True

    async def replay_interaction_delivery(self, _ctx: Any) -> None:
        return None

    async def finish_interaction_execution(
        self,
        _ctx: Any,
        *,
        execution_status: str,
        execution_error: Optional[str] = None,
    ) -> None:
        _ = execution_status, execution_error

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )


def _build_recovery_service(
    *,
    store: DiscordStateStore,
    rest: _ChaosRest,
    operation_store: SQLiteChatOperationLedger,
) -> DiscordBotService:
    service = DiscordBotService.__new__(DiscordBotService)
    service._store = store
    service._chat_operation_store = operation_store
    service._rest = rest
    service._config = SimpleNamespace(
        application_id="app-1",
        max_message_length=2000,
        root=Path("."),
    )
    service._logger = logging.getLogger("test.discord.chaos.recovery")
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


async def _delayed_send(
    responder: DiscordResponder,
    *,
    delay_seconds: float,
    interaction_id: str,
    interaction_token: str,
    deferred: bool,
    text: str,
    ephemeral: bool,
) -> None:
    await asyncio.sleep(delay_seconds)
    await responder.send_or_respond(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
        ephemeral=ephemeral,
    )


@pytest.mark.anyio
async def test_chaos_duplicate_delivery_and_restart_keep_single_ack(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _IngressStoreService(harness.store)
        ingress = InteractionIngress(service, logger=service._logger)
        payload = _slash_payload(
            interaction_id="chaos-dup-1",
            interaction_token="token-chaos-dup-1",
        )

        first = await ingress.process_raw_payload(payload)
        assert first.accepted is True

        responder = harness.responder()
        deferred = await responder.defer(
            interaction_id="chaos-dup-1",
            interaction_token="token-chaos-dup-1",
            ephemeral=True,
        )
        assert deferred is True

        duplicate = await ingress.process_raw_payload(payload)
        restarted = harness.responder()
        await restarted.send_or_respond(
            interaction_id="chaos-dup-1",
            interaction_token="token-chaos-dup-1",
            deferred=True,
            text="Recovered after restart.",
            ephemeral=True,
        )

        assert duplicate.accepted is False
        assert duplicate.rejection_reason == "duplicate_interaction"
        assert len(service.prepare_command_calls) == 0
        assert len(harness.rest.interaction_responses) == 1
        assert len(harness.rest.followup_messages) == 1
        assert (
            harness.rest.followup_messages[0]["payload"]["content"]
            == "Recovered after restart."
        )

        record = await harness.store.get_interaction("chaos-dup-1")
        assert record is not None
        assert record.ack_mode == "defer_ephemeral"
        assert record.final_delivery_status == "followup_sent"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_callback_failure_before_defer_does_not_persist_ack(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        await harness.register_interaction(
            interaction_id="chaos-pre-defer-1",
            interaction_token="token-chaos-pre-defer-1",
        )
        harness.rest.inject_fault(
            "create_interaction_response",
            exc=RuntimeError("callback offline"),
        )
        responder = harness.responder()

        deferred = await responder.defer(
            interaction_id="chaos-pre-defer-1",
            interaction_token="token-chaos-pre-defer-1",
            ephemeral=True,
        )

        assert deferred is False
        record = await harness.store.get_interaction("chaos-pre-defer-1")
        assert record is not None
        assert record.ack_mode is None
        assert record.final_delivery_status == "ack_failed"
        assert "callback offline" in str(record.final_delivery_error)

        retried = harness.responder()
        await retried.respond(
            "chaos-pre-defer-1",
            "token-chaos-pre-defer-1",
            "Recovered after callback failure.",
            ephemeral=True,
        )

        record = await harness.store.get_interaction("chaos-pre-defer-1")
        assert record is not None
        assert record.ack_mode == "immediate_message"
        assert record.final_delivery_status == "initial_response_sent"
        assert len(harness.rest.interaction_responses) == 2
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_followup_failure_after_defer_recovers_after_restart(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        await harness.register_interaction(
            interaction_id="chaos-post-defer-1",
            interaction_token="token-chaos-post-defer-1",
        )
        responder = harness.responder()
        deferred = await responder.defer(
            interaction_id="chaos-post-defer-1",
            interaction_token="token-chaos-post-defer-1",
            ephemeral=True,
        )
        assert deferred is True

        harness.rest.inject_fault(
            "create_followup_message",
            delay_seconds=0.01,
            exc=RuntimeError("followup offline"),
        )
        sent = await responder.send_followup(
            interaction_id="chaos-post-defer-1",
            interaction_token="token-chaos-post-defer-1",
            content="Delayed business error.",
            ephemeral=True,
        )

        assert sent is False
        failed_record = await harness.store.get_interaction("chaos-post-defer-1")
        assert failed_record is not None
        assert failed_record.ack_mode == "defer_ephemeral"
        assert failed_record.final_delivery_status == "followup_failed"
        assert "followup offline" in str(failed_record.final_delivery_error)

        restarted = harness.responder()
        recovered = await restarted.send_followup(
            interaction_id="chaos-post-defer-1",
            interaction_token="token-chaos-post-defer-1",
            content="Recovered business error.",
            ephemeral=True,
        )

        assert recovered is True
        assert len(harness.rest.interaction_responses) == 1
        assert len(harness.rest.followup_messages) == 2
        assert harness.rest.followup_messages[0]["payload"]["content"] == (
            "Delayed business error."
        )
        assert harness.rest.followup_messages[1]["payload"]["content"] == (
            "Recovered business error."
        )
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_delayed_timeout_followup_and_late_business_error_never_reack(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        await harness.register_interaction(
            interaction_id="chaos-timeout-1",
            interaction_token="token-chaos-timeout-1",
        )
        responder = harness.responder()
        await responder.defer(
            interaction_id="chaos-timeout-1",
            interaction_token="token-chaos-timeout-1",
            ephemeral=True,
        )

        harness.rest.inject_fault("create_followup_message", delay_seconds=0.03)
        await asyncio.gather(
            responder.send_or_respond(
                interaction_id="chaos-timeout-1",
                interaction_token="token-chaos-timeout-1",
                deferred=True,
                text="Command timed out. Please try again later.",
                ephemeral=True,
            ),
            _delayed_send(
                responder,
                delay_seconds=0.005,
                interaction_id="chaos-timeout-1",
                interaction_token="token-chaos-timeout-1",
                deferred=True,
                text="Workspace is already busy.",
                ephemeral=True,
            ),
        )

        record = await harness.store.get_interaction("chaos-timeout-1")
        assert record is not None
        assert record.ack_mode == "defer_ephemeral"
        assert len(harness.rest.interaction_responses) == 1
        assert len(harness.rest.followup_messages) == 2
        assert [
            call["payload"]["content"] for call in harness.rest.followup_messages
        ] == [
            "Command timed out. Please try again later.",
            "Workspace is already busy.",
        ]
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_double_ack_raises_before_second_callback(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        await harness.register_interaction(
            interaction_id="chaos-double-ack-1",
            interaction_token="token-chaos-double-ack-1",
        )
        responder = harness.responder()

        first = await responder.defer(
            interaction_id="chaos-double-ack-1",
            interaction_token="token-chaos-double-ack-1",
            ephemeral=True,
        )
        assert first is True

        with pytest.raises(
            InteractionSessionError,
            match="not allowed after defer_ephemeral",
        ):
            await responder.defer(
                interaction_id="chaos-double-ack-1",
                interaction_token="token-chaos-double-ack-1",
                ephemeral=True,
            )

        assert len(harness.rest.interaction_responses) == 1
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_resumes_acked_not_executed_without_second_ack(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-ack-1",
            interaction_token="token-recover-ack-1",
        )
        payload = _slash_payload(
            interaction_id="recover-ack-1",
            interaction_token="token-recover-ack-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                "state": "completed",
                "operation": "defer_ephemeral",
                "ack_mode_hint": "defer_ephemeral",
                "payload": {"ephemeral": True},
            },
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_awaited_once()
        assert harness.rest.interaction_responses == []

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.execution_status == "completed"
        assert record.attempt_count >= 1
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_replays_delivery_without_rerunning_business_logic(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-delivery-1",
            interaction_token="token-recover-delivery-1",
        )
        payload = _slash_payload(
            interaction_id="recover-delivery-1",
            interaction_token="token-recover-delivery-1",
        )
        await harness.store.register_interaction(
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
            scheduler_state="delivery_pending",
        )
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="completed",
        )
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                "state": "pending",
                "operation": "send_followup",
                "payload": {
                    "content": "Recovered final message.",
                    "ephemeral": True,
                    "components": None,
                },
            },
            scheduler_state="delivery_pending",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        assert len(harness.rest.followup_messages) == 1
        assert (
            harness.rest.followup_messages[0]["payload"]["content"]
            == "Recovered final message."
        )

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.execution_status == "completed"
        assert record.final_delivery_status == "followup_sent"
        assert record.scheduler_state == "completed"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_skips_execution_replay_during_backoff_window(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-backoff-1",
            interaction_token="token-recover-backoff-1",
        )
        payload = _slash_payload(
            interaction_id="recover-backoff-1",
            interaction_token="token-recover-backoff-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_scheduler_state(
            ctx.interaction_id,
            scheduler_state="recovery_scheduled",
            increment_attempt_count=True,
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "recovery_scheduled"
        assert record.attempt_count == 1
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_abandons_unchanged_delivery_cursor_after_budget(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-unchanged-cursor-1",
            interaction_token="token-recover-unchanged-cursor-1",
        )
        payload = _slash_payload(
            interaction_id="recover-unchanged-cursor-1",
            interaction_token="token-recover-unchanged-cursor-1",
        )
        await harness.store.register_interaction(
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
            scheduler_state="delivery_pending",
        )
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="completed",
        )
        base_cursor = {
            "state": "pending",
            "operation": "send_followup",
            "payload": {"content": "Recovered final message.", "ephemeral": True},
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(
                base_cursor,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                **base_cursor,
                _INTERACTION_RECOVERY_METADATA_KEY: {
                    "snapshot_hash": snapshot_hash,
                    "unchanged_attempts": 3,
                },
            },
            scheduler_state="delivery_pending",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        assert harness.rest.followup_messages == []
        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "abandoned"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_duplicate_completed_interaction_is_rejected_without_rerun(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-dup-done-1",
            interaction_token="token-recover-dup-done-1",
        )
        payload = _slash_payload(
            interaction_id="recover-dup-done-1",
            interaction_token="token-recover-dup-done-1",
        )
        await harness.store.register_interaction(
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
            scheduler_state="completed",
        )
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="completed",
        )
        await harness.store.record_interaction_delivery(
            ctx.interaction_id,
            delivery_status="followup_sent",
        )
        await harness.store.mark_interaction_scheduler_state(
            ctx.interaction_id,
            scheduler_state="completed",
        )

        ingress = InteractionIngress(service, logger=service._logger)
        duplicate = await ingress.process_raw_payload(payload)

        assert duplicate.accepted is False
        assert duplicate.rejection_reason == "duplicate_interaction"
        service._handle_car_command.assert_not_awaited()
        assert harness.rest.interaction_responses == []
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_replays_execution_when_defer_ack_hint_is_pending(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )

        async def _recovered_handler(
            interaction_id: str,
            interaction_token: str,
            **_kwargs: Any,
        ) -> None:
            await service._responder.send_or_respond(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=True,
                text="Recovered after deferred ACK hint.",
                ephemeral=True,
            )

        service._handle_car_command.side_effect = _recovered_handler
        ctx = _make_ctx(
            interaction_id="recover-pending-ack-1",
            interaction_token="token-recover-pending-ack-1",
        )
        payload = _slash_payload(
            interaction_id="recover-pending-ack-1",
            interaction_token="token-recover-pending-ack-1",
        )
        await harness.store.register_interaction(
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
            scheduler_state="dispatch_ack_pending",
        )
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                "state": "pending",
                "operation": "defer_ephemeral",
                "ack_mode_hint": "defer_ephemeral",
                "payload": {"ephemeral": True},
            },
            scheduler_state="delivery_pending",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_awaited_once()
        assert harness.rest.interaction_responses == []
        assert len(harness.rest.followup_messages) == 1

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.execution_status == "completed"
        assert record.scheduler_state == "completed"
        assert record.delivery_cursor_json is None
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_replays_public_anchor_delivery_after_completed_execution(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-public-anchor-1",
            interaction_token="token-recover-public-anchor-1",
        )
        payload = _slash_payload(
            interaction_id="recover-public-anchor-1",
            interaction_token="token-recover-public-anchor-1",
            subcommand_name="new",
        )
        await harness.store.register_interaction(
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
            dispatch_ack_policy="defer_public",
        )
        await service._persist_runtime_interaction(
            envelope,
            payload,
            scheduler_state="delivery_pending",
        )
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_public",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="completed",
        )
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                "state": "pending",
                "operation": "edit_original_component_message",
                "payload": {"text": "Recovered final public message."},
            },
            scheduler_state="delivery_pending",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        assert harness.rest.followup_messages == []
        assert len(harness.rest.edited_original_responses) == 1
        assert (
            harness.rest.edited_original_responses[0]["payload"]["content"]
            == "Recovered final public message."
        )

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.execution_status == "completed"
        assert record.final_delivery_status == "original_response_edited"
        assert record.scheduler_state == "completed"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_successful_immediate_response_clears_delivery_cursor_and_recovery_set(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )

        async def _immediate_handler(
            interaction_id: str,
            interaction_token: str,
            **_kwargs: Any,
        ) -> None:
            await service._responder.send_or_respond(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=False,
                text="Immediate reply.",
                ephemeral=True,
            )

        service._handle_car_command.side_effect = _immediate_handler
        ctx = _make_ctx(
            interaction_id="recover-complete-1",
            interaction_token="token-recover-complete-1",
            deferred=False,
        )
        payload = _slash_payload(
            interaction_id="recover-complete-1",
            interaction_token="token-recover-complete-1",
        )
        await harness.store.register_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            interaction_kind=ctx.kind.value,
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            metadata_json=service._interaction_ledger_metadata(ctx),
        )

        service._command_runner.submit(ctx, payload)
        await service._command_runner.shutdown(grace_seconds=2.0)

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.execution_status == "completed"
        assert record.scheduler_state == "completed"
        assert record.delivery_cursor_json is None
        assert await harness.store.list_recoverable_interactions() == []
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_scheduler_serializes_same_workspace_and_same_conversation() -> (
    None
):
    service = _RunnerService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )

    rng = random.Random(180)
    active_counts: dict[str, int] = {}
    overlaps: list[tuple[str, str]] = []
    started_ids: list[str] = []
    finished_ids: list[str] = []
    interaction_keys: dict[str, tuple[str, ...]] = {}

    async def handler(*args: Any, **_kwargs: Any) -> None:
        interaction_id = str(args[0]) if args else ""
        started_ids.append(interaction_id)
        keys = interaction_keys[interaction_id]
        for key in keys:
            active_counts[key] = active_counts.get(key, 0) + 1
            if active_counts[key] > 1:
                overlaps.append((interaction_id, key))
        await asyncio.sleep(rng.uniform(0.005, 0.03))
        for key in keys:
            active_counts[key] -= 1
            if active_counts[key] == 0:
                active_counts.pop(key, None)
        finished_ids.append(interaction_id)

    service._handle_car_command.side_effect = handler

    cases = [
        ("mix-1", "chan-1", "guild-1", ("conversation:discord:chan-1:guild-1",)),
        (
            "mix-2",
            "chan-2",
            "guild-1",
            ("conversation:discord:chan-2:guild-1", "workspace:/tmp/ws-a"),
        ),
        (
            "mix-3",
            "chan-3",
            "guild-1",
            ("conversation:discord:chan-3:guild-1", "workspace:/tmp/ws-a"),
        ),
        ("mix-4", "chan-1", "guild-1", ("conversation:discord:chan-1:guild-1",)),
        (
            "mix-5",
            "chan-4",
            "guild-1",
            ("conversation:discord:chan-4:guild-1", "workspace:/tmp/ws-b"),
        ),
        (
            "mix-6",
            "chan-5",
            "guild-1",
            ("conversation:discord:chan-5:guild-1", "workspace:/tmp/ws-b"),
        ),
    ]
    rng.shuffle(cases)

    for interaction_id, channel_id, guild_id, resource_keys in cases:
        ctx = _make_ctx(
            interaction_id=interaction_id,
            interaction_token=f"token-{interaction_id}",
            channel_id=channel_id,
            guild_id=guild_id,
        )
        interaction_keys[interaction_id] = resource_keys
        runner.submit(
            ctx,
            _slash_payload(
                interaction_id=interaction_id,
                interaction_token=f"token-{interaction_id}",
                channel_id=channel_id,
                guild_id=guild_id,
            ),
            resource_keys=resource_keys,
            conversation_id=conversation_id_for("discord", channel_id, guild_id),
        )

    await runner.shutdown(grace_seconds=5.0)

    assert overlaps == []
    assert set(started_ids) == {case[0] for case in cases}
    assert set(finished_ids) == {case[0] for case in cases}


@pytest.mark.anyio
async def test_chaos_shutdown_cancels_pending_same_workspace_interaction() -> None:
    service = _RunnerService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started_ids: list[str] = []

    async def blocking_handler(*args: Any, **_kwargs: Any) -> None:
        interaction_id = str(args[0]) if args else ""
        started_ids.append(interaction_id)
        if interaction_id == "pending-1":
            first_started.set()
            await release_first.wait()

    service._handle_car_command.side_effect = blocking_handler

    runner.submit(
        _make_ctx(
            interaction_id="pending-1",
            interaction_token="token-pending-1",
            channel_id="chan-1",
        ),
        _slash_payload(
            interaction_id="pending-1",
            interaction_token="token-pending-1",
            channel_id="chan-1",
        ),
        resource_keys=(
            "conversation:discord:chan-1:guild-1",
            "workspace:/tmp/ws-c",
        ),
        conversation_id="discord:chan-1:guild-1",
    )
    runner.submit(
        _make_ctx(
            interaction_id="pending-2",
            interaction_token="token-pending-2",
            channel_id="chan-2",
        ),
        _slash_payload(
            interaction_id="pending-2",
            interaction_token="token-pending-2",
            channel_id="chan-2",
        ),
        resource_keys=(
            "conversation:discord:chan-2:guild-1",
            "workspace:/tmp/ws-c",
        ),
        conversation_id="discord:chan-2:guild-1",
    )

    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await asyncio.sleep(0.02)

    shutdown_task = asyncio.create_task(runner.shutdown(grace_seconds=0.01))
    await asyncio.sleep(0.02)
    release_first.set()
    await shutdown_task

    assert started_ids == ["pending-1"]


@pytest.mark.anyio
async def test_recovery_delivery_expired_when_initial_ack_not_durable(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="chaos-expired-no-ack-1",
            interaction_token="token-chaos-expired-no-ack-1",
        )
        payload = _slash_payload(
            interaction_id="chaos-expired-no-ack-1",
            interaction_token="token-chaos-expired-no-ack-1",
        )
        await harness.store.register_interaction(
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
        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "delivery_expired"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_delivery_expired_logs_expired_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="chaos-expired-log-1",
            interaction_token="token-chaos-expired-log-1",
        )
        payload = _slash_payload(
            interaction_id="chaos-expired-log-1",
            interaction_token="token-chaos-expired-log-1",
        )
        await harness.store.register_interaction(
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

        with caplog.at_level(logging.WARNING, logger=service._logger.name):
            await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        events = _logged_events(caplog, service._logger.name)
        assert events[-1]["event"] == "discord.interaction.recovery.delivery_expired"
        assert events[-1]["reason"] == "initial_ack_not_durable"
        assert events[-1]["scheduler_state"] == "delivery_expired"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_abandoned_when_envelope_is_missing(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="chaos-abandoned-no-envelope-1",
            interaction_token="token-chaos-abandoned-no-envelope-1",
        )
        payload = _slash_payload(
            interaction_id="chaos-abandoned-no-envelope-1",
            interaction_token="token-chaos-abandoned-no-envelope-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="running",
        )

        def _null_envelope() -> None:
            conn = harness.store._connection_sync()
            conn.execute(
                "UPDATE interaction_ledger SET envelope_json = NULL WHERE interaction_id = ?",
                (ctx.interaction_id,),
            )
            conn.commit()

        await harness.store._run(_null_envelope)

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "abandoned"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_missing_payload_logs_payload_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="chaos-missing-payload-log-1",
            interaction_token="token-chaos-missing-payload-log-1",
        )
        payload = _slash_payload(
            interaction_id="chaos-missing-payload-log-1",
            interaction_token="token-chaos-missing-payload-log-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="running",
        )

        def _null_payload() -> None:
            conn = harness.store._connection_sync()
            conn.execute(
                "UPDATE interaction_ledger SET payload_json = NULL WHERE interaction_id = ?",
                (ctx.interaction_id,),
            )
            conn.commit()

        await harness.store._run(_null_payload)

        with caplog.at_level(logging.ERROR, logger=service._logger.name):
            await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        events = _logged_events(caplog, service._logger.name)
        assert events[-1]["event"] == "discord.interaction.recovery.abandoned"
        assert events[-1]["reason"] == "missing_runtime_payload"
        assert events[-1]["scheduler_state"] == "abandoned"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_handles_multiple_pending_interactions_independently(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        interactions: list[tuple[str, str, str]] = [
            ("multi-1", "token-multi-1", "conversation:discord:chan-1:guild-1"),
            ("multi-2", "token-multi-2", "conversation:discord:chan-2:guild-1"),
            ("multi-3", "token-multi-3", "conversation:discord:chan-3:guild-1"),
        ]
        for iid, token, _conv in interactions:
            ctx = _make_ctx(interaction_id=iid, interaction_token=token)
            payload = _slash_payload(interaction_id=iid, interaction_token=token)
            await harness.store.register_interaction(
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
                conversation_id=_conv,
                resource_keys=(_conv,),
                dispatch_ack_policy="defer_ephemeral",
            )
            await service._persist_runtime_interaction(
                envelope,
                payload,
                scheduler_state="delivery_pending",
            )
            await harness.store.mark_interaction_acknowledged(
                ctx.interaction_id,
                ack_mode="defer_ephemeral",
            )
            await harness.store.mark_interaction_execution(
                ctx.interaction_id,
                execution_status="completed",
            )
            await harness.store.update_interaction_delivery_cursor(
                ctx.interaction_id,
                delivery_cursor_json={
                    "state": "pending",
                    "operation": "send_followup",
                    "payload": {
                        "content": f"Recovered {iid}.",
                        "ephemeral": True,
                    },
                },
                scheduler_state="delivery_pending",
            )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        assert len(harness.rest.followup_messages) == 3
        assert sorted(
            msg["payload"]["content"] for msg in harness.rest.followup_messages
        ) == ["Recovered multi-1.", "Recovered multi-2.", "Recovered multi-3."]

        for iid, _, _ in interactions:
            record = await harness.store.get_interaction(iid)
            assert record is not None
            assert record.scheduler_state == "completed"
            assert record.execution_status == "completed"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_does_not_block_new_incoming_interaction(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )

        async def _recovered_handler(
            interaction_id: str,
            interaction_token: str,
            **_kwargs: Any,
        ) -> None:
            await service._responder.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content="Recovered execution.",
                ephemeral=True,
            )

        service._handle_car_command.side_effect = _recovered_handler
        recover_ctx = _make_ctx(
            interaction_id="recover-old-1",
            interaction_token="token-recover-old-1",
        )
        recover_payload = _slash_payload(
            interaction_id="recover-old-1",
            interaction_token="token-recover-old-1",
        )
        await harness.store.register_interaction(
            interaction_id=recover_ctx.interaction_id,
            interaction_token=recover_ctx.interaction_token,
            interaction_kind=recover_ctx.kind.value,
            channel_id=recover_ctx.channel_id,
            guild_id=recover_ctx.guild_id,
            user_id=recover_ctx.user_id,
            metadata_json=service._interaction_ledger_metadata(recover_ctx),
        )
        envelope = RuntimeInteractionEnvelope(
            context=recover_ctx,
            conversation_id="conversation:discord:chan-1:guild-1",
            resource_keys=("conversation:discord:chan-1:guild-1",),
            dispatch_ack_policy="defer_ephemeral",
        )
        await service._persist_runtime_interaction(
            envelope,
            recover_payload,
            scheduler_state="acknowledged",
        )
        await harness.store.mark_interaction_acknowledged(
            recover_ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            recover_ctx.interaction_id,
            execution_status="acknowledged",
        )

        await service._resume_interaction_recovery()

        new_ctx = _make_ctx(
            interaction_id="new-fresh-1",
            interaction_token="token-new-fresh-1",
        )
        new_payload = _slash_payload(
            interaction_id="new-fresh-1",
            interaction_token="token-new-fresh-1",
        )
        await harness.store.register_interaction(
            interaction_id=new_ctx.interaction_id,
            interaction_token=new_ctx.interaction_token,
            interaction_kind=new_ctx.kind.value,
            channel_id=new_ctx.channel_id,
            guild_id=new_ctx.guild_id,
            user_id=new_ctx.user_id,
            metadata_json=service._interaction_ledger_metadata(new_ctx),
        )

        ingress = InteractionIngress(service, logger=service._logger)
        result = await ingress.process_raw_payload(new_payload)
        assert result.accepted is True

        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_awaited_once()
        recovered = await harness.store.get_interaction("recover-old-1")
        assert recovered is not None
        assert recovered.scheduler_state == "completed"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_recovery_replays_respond_message_delivery_cursor(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="recover-respond-1",
            interaction_token="token-recover-respond-1",
            deferred=False,
        )
        payload = _slash_payload(
            interaction_id="recover-respond-1",
            interaction_token="token-recover-respond-1",
        )
        await harness.store.register_interaction(
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
            scheduler_state="delivery_pending",
        )
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="completed",
        )
        await harness.store.update_interaction_delivery_cursor(
            ctx.interaction_id,
            delivery_cursor_json={
                "state": "pending",
                "operation": "respond_message",
                "payload": {
                    "text": "Recovered via respond_message.",
                    "ephemeral": True,
                },
            },
            scheduler_state="delivery_pending",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        total_deliveries = (
            len(harness.rest.interaction_responses)
            + len(harness.rest.followup_messages)
            + len(harness.rest.edited_original_responses)
        )
        assert total_deliveries == 1

        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "completed"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_scheduler_state_transitions_from_acknowledged_to_completed(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )

        async def _completing_handler(
            interaction_id: str,
            interaction_token: str,
            **_kwargs: Any,
        ) -> None:
            await service._responder.send_followup(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                content="Handler completed.",
                ephemeral=True,
            )

        service._handle_car_command.side_effect = _completing_handler
        ctx = _make_ctx(
            interaction_id="transition-1",
            interaction_token="token-transition-1",
        )
        payload = _slash_payload(
            interaction_id="transition-1",
            interaction_token="token-transition-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_acknowledged(
            ctx.interaction_id,
            ack_mode="defer_ephemeral",
        )
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="acknowledged",
        )

        record_before = await harness.store.get_interaction(ctx.interaction_id)
        assert record_before is not None
        assert record_before.scheduler_state == "acknowledged"
        assert record_before.execution_status == "acknowledged"

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_awaited_once()
        record_after = await harness.store.get_interaction(ctx.interaction_id)
        assert record_after is not None
        assert record_after.scheduler_state == "completed"
        assert record_after.execution_status == "completed"
        assert record_after.final_delivery_status == "followup_sent"
    finally:
        await harness.close()


@pytest.mark.anyio
async def test_chaos_concurrent_submit_and_graceful_shutdown_drains_running(
    tmp_path: Path,
) -> None:
    service = _RunnerService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=None, stalled_warning_seconds=None),
        logger=service._logger,
    )

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_calls: list[str] = []

    async def _blocking_handler(
        interaction_id: str,
        interaction_token: str,
        **_kwargs: Any,
    ) -> None:
        handler_calls.append(interaction_id)
        handler_started.set()
        await release_handler.wait()

    service._handle_car_command.side_effect = _blocking_handler

    ctx = _make_ctx(
        interaction_id="drain-1",
        interaction_token="token-drain-1",
    )
    runner.submit(
        ctx,
        _slash_payload(
            interaction_id="drain-1",
            interaction_token="token-drain-1",
        ),
    )

    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    shutdown_task = asyncio.create_task(runner.shutdown(grace_seconds=2.0))
    await asyncio.sleep(0.02)

    release_handler.set()
    await shutdown_task

    assert handler_calls == ["drain-1"]


@pytest.mark.anyio
async def test_recovery_delivery_expired_when_ack_mode_is_missing_and_no_pending_delivery(
    tmp_path: Path,
) -> None:
    harness = _ChaosHarness(tmp_path)
    await harness.initialize()
    try:
        service = _build_recovery_service(
            store=harness.store,
            rest=harness.rest,
            operation_store=harness.operation_store,
        )
        ctx = _make_ctx(
            interaction_id="chaos-expired-no-ackmode-1",
            interaction_token="token-chaos-expired-no-ackmode-1",
        )
        payload = _slash_payload(
            interaction_id="chaos-expired-no-ackmode-1",
            interaction_token="token-chaos-expired-no-ackmode-1",
        )
        await harness.store.register_interaction(
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
        await harness.store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status="acknowledged",
        )

        await service._resume_interaction_recovery()
        await service._command_runner.shutdown(grace_seconds=2.0)

        service._handle_car_command.assert_not_awaited()
        record = await harness.store.get_interaction(ctx.interaction_id)
        assert record is not None
        assert record.scheduler_state == "delivery_expired"
    finally:
        await harness.close()
