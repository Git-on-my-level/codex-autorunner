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

Handler timeout enforcement is provided by the ChatDispatcher (configured
via ``dispatch.handler_timeout_seconds``, defaulting to 120 s).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Set

from ...core.logging_utils import log_event
from .ingress import IngressContext, IngressTiming
from .interaction_dispatch import (
    execute_ingressed_interaction,
)

DEFAULT_HANDLER_TIMEOUT_SECONDS: float = 120.0
DEFAULT_STALLED_WARNING_SECONDS: Optional[float] = 60.0

_DRAIN_SENTINEL: object = object()


@dataclass(frozen=True)
class QueuedIngressInteraction:
    ctx: IngressContext
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: float = DEFAULT_HANDLER_TIMEOUT_SECONDS
    stalled_warning_seconds: Optional[float] = DEFAULT_STALLED_WARNING_SECONDS


class CommandRunner:
    def __init__(
        self,
        service: Any,
        *,
        config: RunnerConfig,
        logger: logging.Logger,
    ) -> None:
        self._service = service
        self._config = config
        self._logger = logger
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task[None]] = None
        self._direct_tasks: Set[asyncio.Task[None]] = set()
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
        count = len(self._direct_tasks)
        if self._drain_task is not None and not self._drain_task.done():
            count += 1
        return count

    def submit(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._run_with_lifecycle(ctx, payload),
            name=f"discord-runner-{ctx.interaction_id}",
        )
        self._direct_tasks.add(task)
        task.add_done_callback(self._direct_task_done)

    def submit_event(self, event: Any) -> None:
        self._ensure_started()
        self._queue.put_nowait(event)

    def submit_ingressed(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
    ) -> None:
        self._ensure_started()
        self._queue.put_nowait(QueuedIngressInteraction(ctx=ctx, payload=payload))

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
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
        if self._direct_tasks:
            tasks = list(self._direct_tasks)
            done, pending = await asyncio.wait(
                tasks, timeout=grace_seconds, return_when=asyncio.ALL_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._direct_tasks.clear()
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    async def _drain_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is _DRAIN_SENTINEL:
                    self._queue.task_done()
                    return
                try:
                    started_at = time.monotonic()
                    if isinstance(item, QueuedIngressInteraction):
                        log_event(
                            self._logger,
                            logging.DEBUG,
                            "discord.runner.execute.start",
                            interaction_id=item.ctx.interaction_id,
                            kind=item.ctx.kind.value,
                            command=(
                                ":".join(item.ctx.command_spec.path)
                                if item.ctx.command_spec
                                else item.ctx.kind.value
                            ),
                        )
                        await self._run_with_lifecycle(item.ctx, item.payload)
                    else:
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

    def _direct_task_done(self, task: asyncio.Task[None]) -> None:
        self._direct_tasks.discard(task)
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
                await asyncio.wait_for(
                    asyncio.shield(handler_task),
                    timeout=self._config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._handle_timeout(handler_task, ctx, started_at)
                return
        except asyncio.CancelledError:
            if handler_task is not None and not handler_task.done():
                handler_task.cancel()
            raise
        except Exception as exc:
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
        except Exception:
            pass

    async def _send_error_followup(self, ctx: IngressContext) -> None:
        try:
            await self._service._send_or_respond_ephemeral(
                interaction_id=ctx.interaction_id,
                interaction_token=ctx.interaction_token,
                deferred=ctx.deferred,
                text="An unexpected error occurred. Please try again later.",
            )
        except Exception:
            pass

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
