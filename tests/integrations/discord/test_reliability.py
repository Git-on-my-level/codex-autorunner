"""Reliability and failure-focused tests for the Discord interaction runtime.

These tests enforce the invariants that motivated the interaction runtime
refactor:

- The gateway worker must never be blocked by command execution.
- Ack/defer must happen within Discord's 3-second window.
- Handler timeouts are enforced even for unresponsive handlers.
- Queue pressure does not cause lost events.
- Degraded Discord callbacks (followup failures) do not crash the runner.
- Timing telemetry captures the full interaction lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.integrations.discord.command_runner import (
    CommandRunner,
    RunnerConfig,
)
from codex_autorunner.integrations.discord.ingress import (
    CommandSpec,
    IngressTiming,
    InteractionIngress,
    InteractionKind,
)

DISCORD_ACK_WINDOW_SECONDS = 3.0


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
) -> Any:
    from codex_autorunner.integrations.discord.ingress import IngressContext

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
        self._logger = logging.getLogger("test.reliability")
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


class _FakeIngressService:
    def __init__(
        self,
        *,
        command_allowed: bool = True,
        prepared_policy: Optional[str] = None,
        ack_succeeds: bool = True,
    ) -> None:
        self._command_allowed = command_allowed
        self._prepared_policy = prepared_policy
        self._ack_succeeds = ack_succeeds
        self._logger = logging.getLogger("test.reliability.ingress")
        self.respond_ephemeral_calls: list[dict[str, Any]] = []
        self.respond_autocomplete_calls: list[dict[str, Any]] = []
        self.prepare_command_calls: list[dict[str, Any]] = []

    def _evaluate_interaction_collaboration_policy(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> Any:
        from codex_autorunner.integrations.chat.collaboration_policy import (
            CollaborationEvaluationResult,
        )

        if self._command_allowed:
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
        return CollaborationEvaluationResult(
            outcome="denied_destination",
            allowed=False,
            command_allowed=False,
            should_start_turn=False,
            actor_allowed=True,
            container_allowed=True,
            destination_allowed=False,
            destination_mode="denied",
            plain_text_trigger="disabled",
            reason="denied",
        )

    def _log_collaboration_policy_result(self, **kwargs: Any) -> None:
        pass

    async def _respond_ephemeral(
        self, interaction_id: str, interaction_token: str, text: str
    ) -> None:
        self.respond_ephemeral_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "text": text,
            }
        )

    async def _respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        self.respond_autocomplete_calls.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "choices": choices,
            }
        )

    def _prepared_interaction_policy(self, token: str) -> Optional[str]:
        return self._prepared_policy

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
        return self._ack_succeeds

    @staticmethod
    def _normalize_discord_command_path(
        command_path: tuple[str, ...],
    ) -> tuple[str, ...]:
        if command_path[:1] == ("flow",):
            return ("car", "flow", *command_path[1:])
        return command_path


DISCORD_ACK_WINDOW_SECONDS = 3.0


def _make_ctx_with_timing(
    *,
    interaction_id: str = "inter-timed",
    interaction_token: str = "token-timed",
    channel_id: str = "chan-1",
    kind: InteractionKind = InteractionKind.SLASH_COMMAND,
    deferred: bool = True,
    command_path: tuple[str, ...] = ("car", "status"),
    ingress_started_at: Optional[float] = None,
    ingress_finished_at: Optional[float] = None,
    ack_finished_at: Optional[float] = None,
    interaction_created_at: Optional[float] = None,
) -> tuple[Any, dict[str, Any]]:
    import time as _time

    now = _time.monotonic()
    timing = IngressTiming(
        interaction_created_at=interaction_created_at,
        ingress_started_at=ingress_started_at or now,
        authz_finished_at=ack_finished_at or now,
        ack_finished_at=ack_finished_at or now,
        ingress_finished_at=ingress_finished_at or now,
    )
    ctx = _make_ctx(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        channel_id=channel_id,
        kind=kind,
        deferred=deferred,
        command_path=command_path,
    )
    ctx.timing = timing
    payload = _slash_payload()
    payload["id"] = interaction_id
    payload["token"] = interaction_token
    return ctx, payload


@pytest.mark.anyio
async def test_ingress_completes_within_ack_window() -> None:
    """Ingress (normalize + authz + ack) must complete well within
    Discord's 3-second initial response window."""
    service = _FakeIngressService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_payload()

    start = asyncio.get_event_loop().time()
    result = await ingress.process_raw_payload(payload)
    elapsed = asyncio.get_event_loop().time() - start

    assert result.accepted is True
    assert elapsed < DISCORD_ACK_WINDOW_SECONDS


@pytest.mark.anyio
async def test_ingress_timing_monotonically_increases() -> None:
    """All IngressTiming timestamps must be monotonically increasing."""
    service = _FakeIngressService()
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_payload()

    result = await ingress.process_raw_payload(payload)
    assert result.accepted is True
    assert result.context is not None

    t = result.context.timing
    assert t.ingress_started_at is not None
    assert t.authz_finished_at is not None
    assert t.ack_finished_at is not None
    assert t.ingress_finished_at is not None

    assert t.ingress_started_at <= t.authz_finished_at
    assert t.authz_finished_at <= t.ack_finished_at
    assert t.ack_finished_at <= t.ingress_finished_at


@pytest.mark.anyio
async def test_gateway_not_blocked_by_slow_handler() -> None:
    """Submitting a second interaction while the first has a slow handler
    must return immediately (gateway is not blocked)."""
    service = _FakeService()
    slow_done = asyncio.Event()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await slow_done.wait()

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=30.0, stalled_warning_seconds=None),
        logger=service._logger,
    )

    ctx1, payload1 = _make_ctx_with_timing(interaction_id="slow-1")
    runner.submit(ctx1, payload1)
    await asyncio.sleep(0.02)

    submit_start = asyncio.get_event_loop().time()
    ctx2, payload2 = _make_ctx_with_timing(interaction_id="fast-1")
    runner.submit(ctx2, payload2)
    submit_elapsed = asyncio.get_event_loop().time() - submit_start

    assert (
        submit_elapsed < 0.1
    ), f"submit() took {submit_elapsed:.3f}s -- gateway would be blocked"

    slow_done.set()
    await asyncio.sleep(0.05)
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_timeout_enforcement_cancels_handler() -> None:
    """A handler that exceeds the timeout must be cancelled and the user
    notified."""
    service = _FakeService()
    handler_cancelled = False

    async def hung_handler(*args: Any, **kwargs: Any) -> None:
        nonlocal handler_cancelled
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            handler_cancelled = True
            raise

    service._handle_car_command.side_effect = hung_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx, payload = _make_ctx_with_timing()
    runner.submit(ctx, payload)
    await asyncio.sleep(0.3)

    assert handler_cancelled, "Handler was not cancelled on timeout"
    service._send_or_respond_ephemeral.assert_awaited()
    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_timeout_followup_text_mentions_timeout() -> None:
    """The timeout followup message must indicate the command timed out."""
    service = _FakeService()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(300)

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx, payload = _make_ctx_with_timing()
    runner.submit(ctx, payload)
    await asyncio.sleep(0.3)

    service._send_or_respond_ephemeral.assert_awaited_once()
    call_kwargs = service._send_or_respond_ephemeral.call_args[1]
    assert "timed out" in call_kwargs["text"].lower()


@pytest.mark.anyio
async def test_queue_drain_preserves_arrival_order_under_pressure() -> None:
    """When many events are queued rapidly, the drain loop must process
    them in FIFO order."""
    service = _FakeService()
    dispatched_order: list[str] = []

    async def track_dispatch(event: Any) -> None:
        dispatched_order.append(event["label"])

    service._dispatch_chat_event = track_dispatch

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    for i in range(50):
        runner.submit_event({"label": f"event-{i:03d}"})

    await runner.shutdown(grace_seconds=10.0)

    assert len(dispatched_order) == 50
    expected = [f"event-{i:03d}" for i in range(50)]
    assert (
        dispatched_order == expected
    ), f"Order mismatch: expected first 5 {expected[:5]}, got {dispatched_order[:5]}"


@pytest.mark.anyio
async def test_queue_pressure_does_not_lose_events() -> None:
    """Under concurrent submission pressure, no events should be lost."""
    service = _FakeService()
    dispatched_count = 0

    async def count_dispatch(event: Any) -> None:
        nonlocal dispatched_count
        dispatched_count += 1
        await asyncio.sleep(0.001)

    service._dispatch_chat_event = count_dispatch

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    num_events = 100
    for i in range(num_events):
        runner.submit_event({"label": f"e-{i}"})

    await runner.shutdown(grace_seconds=30.0)

    assert (
        dispatched_count == num_events
    ), f"Lost {num_events - dispatched_count} events under pressure"


@pytest.mark.anyio
async def test_degraded_followup_does_not_crash_runner() -> None:
    """If sending the error followup itself fails, the runner must not
    crash."""
    service = _FakeService()

    async def failing_handler(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("handler boom")

    service._handle_car_command.side_effect = failing_handler
    service._respond_ephemeral.side_effect = RuntimeError("followup also boom")

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )
    ctx, payload = _make_ctx_with_timing()
    runner.submit(ctx, payload)
    await asyncio.sleep(0.1)

    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_degraded_timeout_followup_does_not_crash_runner() -> None:
    """If sending the timeout followup itself fails, the runner must not
    crash."""
    service = _FakeService()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(300)

    service._handle_car_command.side_effect = slow_handler
    service._send_or_respond_ephemeral.side_effect = RuntimeError(
        "timeout followup boom"
    )

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )
    ctx, payload = _make_ctx_with_timing()
    runner.submit(ctx, payload)
    await asyncio.sleep(0.3)

    assert runner.active_task_count == 0


@pytest.mark.anyio
async def test_execution_timing_recorded_in_context() -> None:
    """After execution, the IngressContext must have execution_started_at
    and execution_finished_at set."""
    service = _FakeService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    ctx, payload = _make_ctx_with_timing()
    assert ctx.timing.execution_started_at is None
    assert ctx.timing.execution_finished_at is None

    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    assert ctx.timing.execution_started_at is not None
    assert ctx.timing.execution_finished_at is not None
    assert ctx.timing.execution_started_at <= ctx.timing.execution_finished_at


@pytest.mark.anyio
async def test_full_lifecycle_timing_chain() -> None:
    """The full timing chain must be monotonically increasing:
    interaction_created_at <= ingress_started <= authz <= ack <= ingress_finished
    <= execution_started <= execution_finished."""
    service = _FakeService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    import time as _time

    created_at = _time.time() - 0.5
    now = _time.monotonic()
    ctx, payload = _make_ctx_with_timing(interaction_created_at=created_at)
    ctx.timing = IngressTiming(
        interaction_created_at=created_at,
        ingress_started_at=now - 0.1,
        authz_finished_at=now - 0.05,
        ack_finished_at=now - 0.02,
        ingress_finished_at=now - 0.01,
    )

    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    t = ctx.timing
    assert t.interaction_created_at is not None
    assert t.ingress_started_at is not None
    assert t.execution_started_at is not None
    assert t.execution_finished_at is not None

    assert (
        t.ingress_started_at <= (t.authz_finished_at or 0)
        or t.authz_finished_at is None
    )
    if t.authz_finished_at is not None and t.ack_finished_at is not None:
        assert t.authz_finished_at <= t.ack_finished_at
    if t.ack_finished_at is not None and t.ingress_finished_at is not None:
        assert t.ack_finished_at <= t.ingress_finished_at
    assert t.ingress_finished_at is not None
    assert t.ingress_finished_at <= t.execution_started_at
    assert t.execution_started_at <= t.execution_finished_at


@pytest.mark.anyio
async def test_stall_warning_fires_for_slow_handler() -> None:
    """When a handler exceeds the stall warning threshold, a warning must
    be logged."""
    service = _FakeService()

    async def slow_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(10)

    service._handle_car_command.side_effect = slow_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0, stalled_warning_seconds=0.05),
        logger=service._logger,
    )

    stall_events: list[dict[str, Any]] = []
    original_log = service._logger.log

    def capture_log(level: int, msg: str, *args_log: Any, **kwargs_log: Any) -> None:
        try:
            parsed = json.loads(msg)
            if parsed.get("event") == "discord.runner.stalled":
                stall_events.append(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        original_log(level, msg, *args_log, **kwargs_log)

    service._logger.log = capture_log  # type: ignore[assignment]

    ctx, payload = _make_ctx_with_timing()
    runner.submit(ctx, payload)
    await asyncio.sleep(0.15)

    assert len(stall_events) >= 1, "No stall warning was emitted"
    event = stall_events[0]
    assert event["interaction_id"] == "inter-timed"
    assert "elapsed_ms" in event

    await runner.shutdown(grace_seconds=1.0)


@pytest.mark.anyio
async def test_runner_telemetry_emits_lifecycle_metrics() -> None:
    """The execute.done event must include total_lifecycle_ms and
    gateway_to_completion_ms when timing data is available."""
    service = _FakeService()
    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0, stalled_warning_seconds=None),
        logger=service._logger,
    )

    done_events: list[dict[str, Any]] = []
    original_log = service._logger.log

    def capture_log(level: int, msg: str, *args_log: Any, **kwargs_log: Any) -> None:
        try:
            parsed = json.loads(msg)
            if parsed.get("event") == "discord.runner.execute.done":
                done_events.append(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        original_log(level, msg, *args_log, **kwargs_log)

    service._logger.log = capture_log  # type: ignore[assignment]

    import time as _time

    created_at = _time.time() - 0.1
    ctx, payload = _make_ctx_with_timing(interaction_created_at=created_at)

    runner.submit(ctx, payload)
    await asyncio.sleep(0.05)

    assert len(done_events) >= 1
    event = done_events[0]
    assert "total_lifecycle_ms" in event
    assert event["total_lifecycle_ms"] is not None
    assert event["total_lifecycle_ms"] > 0
    assert "gateway_to_completion_ms" in event
    assert event["gateway_to_completion_ms"] is not None


@pytest.mark.anyio
async def test_ingress_timing_includes_snowflake_created_at() -> None:
    """Ingress timing must include the snowflake-derived created_at for
    diagnosing ack misses."""
    service = _FakeIngressService()
    ingress = InteractionIngress(service, logger=service._logger)

    created_at_ms = 1_700_000_000_000
    snowflake = str((created_at_ms - 1420070400000) << 22)
    payload = _slash_payload()
    payload["id"] = snowflake

    result = await ingress.process_raw_payload(payload)
    assert result.accepted is True
    assert result.context is not None
    assert result.context.timing.interaction_created_at is not None

    gateway_to_ingress_ms = (
        result.context.timing.ingress_finished_at
        - result.context.timing.interaction_created_at
        if result.context.timing.ingress_finished_at
        else None
    )
    assert gateway_to_ingress_ms is not None


@pytest.mark.anyio
async def test_multiple_timeouts_in_sequence() -> None:
    """Multiple sequential timeouts must all be handled without leaking
    tasks."""
    service = _FakeService()

    async def forever_handler(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(300)

    service._handle_car_command.side_effect = forever_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=0.05, stalled_warning_seconds=None),
        logger=service._logger,
    )

    for i in range(5):
        ctx, payload = _make_ctx_with_timing(
            interaction_id=f"timeout-{i}",
            interaction_token=f"tok-{i}",
        )
        runner.submit(ctx, payload)

    await asyncio.sleep(0.5)
    assert runner.active_task_count == 0
    assert service._send_or_respond_ephemeral.await_count == 5


@pytest.mark.anyio
async def test_shutdown_cancels_all_in_flight_handlers() -> None:
    """Shutdown must cancel all running handlers and not leave orphan
    tasks."""
    service = _FakeService()
    cancel_count = 0

    async def tracked_handler(*args: Any, **kwargs: Any) -> None:
        nonlocal cancel_count
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            cancel_count += 1
            raise

    service._handle_car_command.side_effect = tracked_handler

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=300.0, stalled_warning_seconds=None),
        logger=service._logger,
    )

    for i in range(3):
        ctx, payload = _make_ctx_with_timing(
            interaction_id=f"inflight-{i}",
            interaction_token=f"tok-{i}",
        )
        runner.submit(ctx, payload)

    await asyncio.sleep(0.05)
    assert runner.active_task_count == 3

    await runner.shutdown(grace_seconds=0.1)
    assert runner.active_task_count == 0
    assert cancel_count == 3


@pytest.mark.anyio
async def test_ingress_rejection_records_timing() -> None:
    """Even when ingress is rejected (e.g. unauthorized), timing must be
    recorded for diagnostics."""
    service = _FakeIngressService(command_allowed=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_payload()

    result = await ingress.process_raw_payload(payload)
    assert result.accepted is False
    assert result.context is not None
    t = result.context.timing
    assert t.ingress_started_at is not None
    assert t.authz_finished_at is not None


@pytest.mark.anyio
async def test_ack_failure_records_timing() -> None:
    """When ack fails, timing must still be captured for diagnosing the
    failure."""
    service = _FakeIngressService(ack_succeeds=False)
    ingress = InteractionIngress(service, logger=service._logger)
    payload = _slash_payload(command_name="car", subcommand_name="status")

    result = await ingress.process_raw_payload(payload)
    assert result.accepted is False
    assert result.rejection_reason == "ack_failed"
    assert result.context is not None
    t = result.context.timing
    assert t.ingress_started_at is not None
    assert t.ack_finished_at is not None


@pytest.mark.anyio
async def test_concurrent_submit_and_shutdown() -> None:
    """Submitting events while shutdown is in progress must not deadlock."""
    service = _FakeService()
    dispatched: list[str] = []

    async def track_dispatch(event: Any) -> None:
        dispatched.append(event["label"])
        await asyncio.sleep(0.01)

    service._dispatch_chat_event = track_dispatch

    runner = CommandRunner(
        service,
        config=RunnerConfig(timeout_seconds=5.0),
        logger=service._logger,
    )

    for i in range(20):
        runner.submit_event({"label": f"e-{i}"})

    shutdown_task = asyncio.create_task(runner.shutdown(grace_seconds=5.0))
    await asyncio.sleep(0.01)

    runner.submit_event({"label": "late-event"})
    await shutdown_task

    assert len(dispatched) >= 1
