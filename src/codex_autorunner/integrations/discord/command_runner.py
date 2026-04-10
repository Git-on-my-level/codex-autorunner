"""Background command runner for Discord interactions.

After ingress has acknowledged an interaction, the runner dispatches the
interaction event off the gateway hot path while preserving arrival order.

The runner maintains an internal FIFO queue drained by a single worker.
Each queued event is dispatched through the service's existing dispatch
chain (``_dispatch_chat_event``) which provides per-conversation ordering.
The runner adds:

- Guaranteed background execution (gateway returns after ingress ack)
- Arrival-order preservation across all submitted events
- A grace-period shutdown that drains in-flight work
- Error logging for runner-level failures

Handler timeout enforcement is optional and should be used sparingly for
interaction handlers because many commands manage their own longer-lived turn
timeouts internally.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Optional, Sequence, Set

from ...core.logging_utils import log_event
from .ingress import IngressContext, IngressTiming
from .interaction_dispatch import (
    execute_ingressed_interaction,
)

DEFAULT_HANDLER_TIMEOUT_SECONDS: Optional[float] = None
DEFAULT_STALLED_WARNING_SECONDS: Optional[float] = 60.0

_DRAIN_SENTINEL: object = object()


@dataclass(frozen=True)
class InteractionSchedule:
    resource_keys: tuple[str, ...] = ()
    conversation_id: Optional[str] = None


@dataclass
class ScheduledInteraction:
    ctx: IngressContext
    payload: dict[str, Any]
    schedule: InteractionSchedule
    fast_ack: Optional[Callable[[IngressContext], Awaitable[bool]]] = None
    admitted: bool = False
    ready: Optional[asyncio.Future[None]] = None


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: Optional[float] = DEFAULT_HANDLER_TIMEOUT_SECONDS
    stalled_warning_seconds: Optional[float] = DEFAULT_STALLED_WARNING_SECONDS


class CommandRunner:
    def __init__(
        self,
        service: Any,
        *,
        config: RunnerConfig,
        logger: logging.Logger,
        on_scheduler_conversation_idle: Optional[
            Callable[[str], Awaitable[None] | None]
        ] = None,
    ) -> None:
        self._service = service
        self._config = config
        self._logger = logger
        self._on_scheduler_conversation_idle = on_scheduler_conversation_idle
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task[None]] = None
        self._interaction_tasks: Set[asyncio.Task[None]] = set()
        self._pending_interactions: Deque[ScheduledInteraction] = deque()
        self._active_resource_keys: set[str] = set()
        self._active_conversation_labels: dict[str, str] = {}
        self._scheduler_lock = asyncio.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._drain_task = asyncio.create_task(
            self._drain_loop(), name="discord-runner-drain"
        )

    @property
    def active_task_count(self) -> int:
        count = len(self._interaction_tasks)
        if self._drain_task is not None and not self._drain_task.done():
            count += 1
        return count

    def submit(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
        *,
        resource_keys: Sequence[str] = (),
        conversation_id: Optional[str] = None,
        fast_ack: Optional[Callable[[IngressContext], Awaitable[bool]]] = None,
    ) -> None:
        schedule = InteractionSchedule(
            resource_keys=tuple(dict.fromkeys(resource_keys)),
            conversation_id=conversation_id,
        )
        item = ScheduledInteraction(
            ctx=ctx,
            payload=payload,
            schedule=schedule,
            fast_ack=fast_ack,
        )
        task = asyncio.create_task(
            self._run_scheduled_interaction(item),
            name=f"discord-runner-{ctx.interaction_id}",
        )
        self._interaction_tasks.add(task)
        task.add_done_callback(self._interaction_task_done)

    def submit_event(self, event: Any) -> None:
        self._ensure_started()
        self._queue.put_nowait(event)

    def is_busy(self, conversation_id: str) -> bool:
        active = self._active_conversation_labels.get(conversation_id)
        if isinstance(active, str) and active.strip():
            return True
        return any(
            item.schedule.conversation_id == conversation_id
            for item in self._pending_interactions
        )

    def describe_busy(self, conversation_id: str) -> Optional[str]:
        active = self._active_conversation_labels.get(conversation_id)
        if isinstance(active, str) and active.strip():
            return active
        for item in self._pending_interactions:
            if item.schedule.conversation_id == conversation_id:
                return self._format_command_label(item.ctx)
        return None

    async def _notify_scheduler_conversations_idle(
        self, conversation_ids: set[str]
    ) -> None:
        if not conversation_ids or self._on_scheduler_conversation_idle is None:
            return
        for conversation_id in conversation_ids:
            maybe_awaitable = self._on_scheduler_conversation_idle(conversation_id)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        idle_conversations: set[str] = set()
        if self._drain_task is not None and not self._drain_task.done():
            await self._queue.put(_DRAIN_SENTINEL)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._drain_task), timeout=grace_seconds
                )
            except asyncio.TimeoutError:
                self._drain_task.cancel()
                with contextlib_suppress(asyncio.CancelledError):
                    await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        if self._interaction_tasks:
            idle_conversations.update(self._active_conversation_labels)
            idle_conversations.update(
                item.schedule.conversation_id
                for item in self._pending_interactions
                if item.schedule.conversation_id
            )
            tasks = list(self._interaction_tasks)
            done, pending = await asyncio.wait(
                tasks, timeout=grace_seconds, return_when=asyncio.ALL_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._interaction_tasks.clear()
        async with self._scheduler_lock:
            self._pending_interactions.clear()
            self._active_resource_keys.clear()
            self._active_conversation_labels.clear()
        await self._notify_scheduler_conversations_idle(
            {
                conversation_id
                for conversation_id in idle_conversations
                if conversation_id
            }
        )
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    @staticmethod
    def _format_command_label(ctx: IngressContext) -> str:
        if ctx.command_spec and ctx.command_spec.path:
            return "/" + " ".join(ctx.command_spec.path)
        if ctx.kind == ctx.kind.COMPONENT and ctx.custom_id:
            return f"component:{ctx.custom_id.split(':', 1)[0]}"
        if ctx.kind == ctx.kind.MODAL_SUBMIT and ctx.custom_id:
            return f"modal:{ctx.custom_id.split(':', 1)[0]}"
        return ctx.kind.value

    async def _drain_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is _DRAIN_SENTINEL:
                    self._queue.task_done()
                    return
                try:
                    started_at = time.monotonic()
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.runner.dispatch.start",
                    )
                    await self._service._dispatch_chat_event(item)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.runner.dispatch.error",
                        exc=exc,
                    )
                finally:
                    self._queue.task_done()
                    elapsed_ms = (time.monotonic() - started_at) * 1000
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.runner.dispatch.done",
                        elapsed_ms=round(elapsed_ms, 1),
                    )
        except asyncio.CancelledError:
            return

    async def _run_scheduled_interaction(self, item: ScheduledInteraction) -> None:
        acquired_schedule = False
        notify_idle: set[str] = set()
        try:
            if item.schedule.resource_keys:
                admitted_immediately = await self._admit_or_enqueue_schedule(item)
                if admitted_immediately:
                    acquired_schedule = True
                else:
                    if item.fast_ack is not None:
                        acknowledged = await item.fast_ack(item.ctx)
                        if not acknowledged:
                            notify_idle = await self._remove_pending_schedule(item)
                            return
                    await self._wait_for_schedule(item)
                    acquired_schedule = True
            await self._run_with_lifecycle(item.ctx, item.payload)
        except asyncio.CancelledError:
            return
        finally:
            if item.schedule.resource_keys and (acquired_schedule or item.admitted):
                notify_idle = await self._release_schedule(item)
            if notify_idle:
                await self._notify_scheduler_conversations_idle(notify_idle)

    async def _admit_or_enqueue_schedule(self, item: ScheduledInteraction) -> bool:
        loop = asyncio.get_running_loop()
        async with self._scheduler_lock:
            has_active_conflict = any(
                key in self._active_resource_keys for key in item.schedule.resource_keys
            )
            has_pending_conflict = any(
                any(
                    key in pending.schedule.resource_keys
                    for key in item.schedule.resource_keys
                )
                for pending in self._pending_interactions
            )
            if not has_active_conflict and not has_pending_conflict:
                self._admit_interaction_locked(item)
                return True
            item.ready = loop.create_future()
            self._pending_interactions.append(item)
            return False

    async def _wait_for_schedule(self, item: ScheduledInteraction) -> None:
        ready = item.ready
        if ready is None:
            return
        try:
            await ready
        except asyncio.CancelledError:
            idle_conversations: set[str] = set()
            async with self._scheduler_lock:
                if item in self._pending_interactions:
                    self._pending_interactions.remove(item)
                    if (
                        item.schedule.conversation_id
                        and not self._conversation_has_pending_or_active_locked(
                            item.schedule.conversation_id
                        )
                    ):
                        idle_conversations.add(item.schedule.conversation_id)
                elif item.admitted:
                    idle_conversations = self._release_schedule_locked(item)
            if idle_conversations:
                await self._notify_scheduler_conversations_idle(idle_conversations)
            raise

    async def _remove_pending_schedule(self, item: ScheduledInteraction) -> set[str]:
        async with self._scheduler_lock:
            if item in self._pending_interactions:
                self._pending_interactions.remove(item)
                item.ready = None
                conversation_id = item.schedule.conversation_id
                if (
                    conversation_id
                    and not self._conversation_has_pending_or_active_locked(
                        conversation_id
                    )
                ):
                    return {conversation_id}
            return set()

    def _activate_pending_locked(self) -> None:
        if not self._pending_interactions:
            return
        remaining: Deque[ScheduledInteraction] = deque()
        for item in self._pending_interactions:
            if any(
                key in self._active_resource_keys for key in item.schedule.resource_keys
            ):
                remaining.append(item)
                continue
            self._admit_interaction_locked(item)
            if item.ready is not None and not item.ready.done():
                item.ready.set_result(None)
        self._pending_interactions = remaining

    def _admit_interaction_locked(self, item: ScheduledInteraction) -> None:
        self._active_resource_keys.update(item.schedule.resource_keys)
        item.admitted = True
        conversation_id = item.schedule.conversation_id
        if conversation_id:
            self._active_conversation_labels[conversation_id] = (
                self._format_command_label(item.ctx)
            )

    async def _release_schedule(self, item: ScheduledInteraction) -> set[str]:
        async with self._scheduler_lock:
            return self._release_schedule_locked(item)

    def _release_schedule_locked(self, item: ScheduledInteraction) -> set[str]:
        for key in item.schedule.resource_keys:
            self._active_resource_keys.discard(key)
        conversation_id = item.schedule.conversation_id
        if conversation_id:
            self._active_conversation_labels.pop(conversation_id, None)
        item.admitted = False
        item.ready = None
        self._activate_pending_locked()
        if conversation_id and not self._conversation_has_pending_or_active_locked(
            conversation_id
        ):
            return {conversation_id}
        return set()

    def _conversation_has_pending_or_active_locked(self, conversation_id: str) -> bool:
        if conversation_id in self._active_conversation_labels:
            return True
        return any(
            item.schedule.conversation_id == conversation_id
            for item in self._pending_interactions
        )

    def _interaction_task_done(self, task: asyncio.Task[None]) -> None:
        self._interaction_tasks.discard(task)
        if task.cancelled():
            return
        exc = None
        try:
            exc = task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return
        if exc is not None and isinstance(exc, Exception):
            log_event(
                self._logger,
                logging.ERROR,
                "discord.runner.task_failed",
                exc=exc,
            )

    async def _run_with_lifecycle(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
    ) -> None:
        begin_execution = getattr(self._service, "_begin_interaction_execution", None)
        finish_execution = getattr(self._service, "_finish_interaction_execution", None)
        claimed_execution = True
        if callable(begin_execution):
            claimed_execution = bool(await begin_execution(ctx))
        if not claimed_execution:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.runner.execute.duplicate_skipped",
                interaction_id=ctx.interaction_id,
                kind=ctx.kind.value,
            )
            return
        started_at = time.monotonic()
        command_label = (
            ":".join(ctx.command_spec.path) if ctx.command_spec else ctx.kind.value
        )
        ingress_elapsed_ms = self._ingress_elapsed_ms(ctx)
        log_event(
            self._logger,
            logging.DEBUG,
            "discord.runner.execute.start",
            interaction_id=ctx.interaction_id,
            kind=ctx.kind.value,
            command=command_label,
            queue_wait_ms=(
                round((started_at - ctx.timing.ingress_finished_at) * 1000, 1)
                if ctx.timing.ingress_finished_at is not None
                else None
            ),
            ingress_elapsed_ms=ingress_elapsed_ms,
        )

        ctx.timing = IngressTiming(
            interaction_created_at=ctx.timing.interaction_created_at,
            ingress_started_at=ctx.timing.ingress_started_at,
            authz_finished_at=ctx.timing.authz_finished_at,
            ack_finished_at=ctx.timing.ack_finished_at,
            ingress_finished_at=ctx.timing.ingress_finished_at,
            execution_started_at=started_at,
        )

        handler_task: Optional[asyncio.Task[None]] = None
        stall_task: Optional[asyncio.Task[None]] = None
        execution_status = "completed"
        execution_error: Optional[str] = None
        try:
            handler_task = asyncio.create_task(
                self._execute_body(ctx, payload),
                name=f"discord-handler-{ctx.interaction_id}",
            )
            if self._config.stalled_warning_seconds is not None:
                stall_task = asyncio.create_task(
                    self._warn_on_stall(handler_task, ctx, started_at),
                )
            try:
                if self._config.timeout_seconds is None:
                    await asyncio.shield(handler_task)
                else:
                    await asyncio.wait_for(
                        asyncio.shield(handler_task),
                        timeout=self._config.timeout_seconds,
                    )
            except asyncio.TimeoutError:
                execution_status = "timeout"
                execution_error = "handler_timeout"
                self._handle_timeout(handler_task, ctx, started_at)
                return
        except asyncio.CancelledError:
            if handler_task is not None and not handler_task.done():
                handler_task.cancel()
            execution_status = "cancelled"
            execution_error = "handler_cancelled"
            raise
        except Exception as exc:
            execution_status = "failed"
            execution_error = str(exc)
            log_event(
                self._logger,
                logging.ERROR,
                "discord.runner.execute.unexpected_error",
                interaction_id=ctx.interaction_id,
                exc=exc,
            )
            await self._send_error_followup(ctx)
        finally:
            if stall_task is not None:
                stall_task.cancel()
                with contextlib_suppress(asyncio.CancelledError):
                    await stall_task
            finished_at = time.monotonic()
            ctx.timing = IngressTiming(
                interaction_created_at=ctx.timing.interaction_created_at,
                ingress_started_at=ctx.timing.ingress_started_at,
                authz_finished_at=ctx.timing.authz_finished_at,
                ack_finished_at=ctx.timing.ack_finished_at,
                ingress_finished_at=ctx.timing.ingress_finished_at,
                execution_started_at=ctx.timing.execution_started_at,
                execution_finished_at=finished_at,
            )
            execution_ms = (finished_at - started_at) * 1000
            total_ms = self._total_lifecycle_ms(ctx, finished_at)
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.runner.execute.done",
                interaction_id=ctx.interaction_id,
                elapsed_ms=round(execution_ms, 1),
                total_lifecycle_ms=round(total_ms, 1) if total_ms is not None else None,
                gateway_to_completion_ms=self._gateway_to_completion_ms(
                    ctx, finished_at
                ),
            )
            if callable(finish_execution):
                await finish_execution(
                    ctx,
                    execution_status=execution_status,
                    execution_error=execution_error,
                )

    async def _execute_body(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
    ) -> None:
        await execute_ingressed_interaction(self._service, ctx, payload)

    def _handle_timeout(
        self,
        handler_task: asyncio.Task[None],
        ctx: IngressContext,
        started_at: float,
    ) -> None:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        command_label = (
            ":".join(ctx.command_spec.path) if ctx.command_spec else ctx.kind.value
        )
        gateway_to_timeout_ms = self._gateway_to_completion_ms(ctx, time.monotonic())
        log_event(
            self._logger,
            logging.ERROR,
            "discord.runner.timeout",
            interaction_id=ctx.interaction_id,
            command=command_label,
            timeout_seconds=self._config.timeout_seconds,
            elapsed_ms=round(elapsed_ms, 1),
            gateway_to_timeout_ms=gateway_to_timeout_ms,
        )
        handler_task.cancel()
        handler_task.add_done_callback(
            lambda t: self._log_cancelled_task_failure(t, ctx)
        )
        asyncio.create_task(self._send_timeout_followup(ctx))

    async def _warn_on_stall(
        self,
        handler_task: asyncio.Task[None],
        ctx: IngressContext,
        started_at: float,
    ) -> None:
        warning_seconds = self._config.stalled_warning_seconds
        if warning_seconds is None:
            return
        try:
            await asyncio.sleep(warning_seconds)
        except asyncio.CancelledError:
            return
        if handler_task.done():
            return
        elapsed_ms = (time.monotonic() - started_at) * 1000
        log_event(
            self._logger,
            logging.WARNING,
            "discord.runner.stalled",
            interaction_id=ctx.interaction_id,
            warning_seconds=warning_seconds,
            elapsed_ms=round(elapsed_ms, 1),
        )

    async def _send_timeout_followup(self, ctx: IngressContext) -> None:
        try:
            await self._service._send_or_respond_ephemeral(
                interaction_id=ctx.interaction_id,
                interaction_token=ctx.interaction_token,
                deferred=ctx.deferred,
                text="Command timed out. Please try again later.",
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.runner.timeout_followup_failed",
                interaction_id=ctx.interaction_id,
                exc=exc,
            )

    async def _send_error_followup(self, ctx: IngressContext) -> None:
        try:
            await self._service._send_or_respond_ephemeral(
                interaction_id=ctx.interaction_id,
                interaction_token=ctx.interaction_token,
                deferred=ctx.deferred,
                text="An unexpected error occurred. Please try again later.",
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.runner.error_followup_failed",
                interaction_id=ctx.interaction_id,
                exc=exc,
            )

    def _log_cancelled_task_failure(
        self, task: asyncio.Task[None], ctx: IngressContext
    ) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return
        if exc is None:
            return
        if isinstance(exc, Exception):
            log_event(
                self._logger,
                logging.ERROR,
                "discord.runner.cancelled_task_error",
                interaction_id=ctx.interaction_id,
                exc=exc,
            )

    @staticmethod
    def _ingress_elapsed_ms(ctx: IngressContext) -> Optional[float]:
        t = ctx.timing
        if t.ingress_started_at is not None and t.ingress_finished_at is not None:
            return round((t.ingress_finished_at - t.ingress_started_at) * 1000, 1)
        return None

    @staticmethod
    def _total_lifecycle_ms(ctx: IngressContext, now: float) -> Optional[float]:
        t = ctx.timing
        if t.ingress_started_at is None:
            return None
        return (now - t.ingress_started_at) * 1000

    @staticmethod
    def _gateway_to_completion_ms(ctx: IngressContext, now: float) -> Optional[float]:
        t = ctx.timing
        if t.interaction_created_at is None:
            return None
        wall_now = time.time()
        return round(max(0.0, (wall_now - t.interaction_created_at) * 1000), 1)


def contextlib_suppress(*exc_types: type) -> Any:
    import contextlib

    return contextlib.suppress(*exc_types)
