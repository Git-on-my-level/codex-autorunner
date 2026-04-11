"""Background command runner for admitted Discord interactions.

After runtime admission has normalized and, when required, acknowledged an
interaction, the runner dispatches it off the gateway hot path while
preserving arrival order.

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
from typing import Any, Awaitable, Callable, Deque, Optional, Protocol, Sequence, Set

from ...core.logging_utils import log_event
from .ingress import IngressContext, IngressTiming, RuntimeInteractionEnvelope
from .interaction_dispatch import (
    execute_ingressed_interaction,
)
from .interaction_registry import DiscordAckPolicy

DEFAULT_HANDLER_TIMEOUT_SECONDS: Optional[float] = None
DEFAULT_STALLED_WARNING_SECONDS: Optional[float] = 60.0

_DRAIN_SENTINEL: object = object()
_SUBMISSION_SENTINEL: object = object()


class DiscordRunnerService(Protocol):
    async def dispatch_chat_event(self, event: Any) -> None: ...

    async def acknowledge_runtime_envelope(
        self,
        envelope: RuntimeInteractionEnvelope,
        *,
        stage: str,
    ) -> bool: ...

    async def mark_interaction_scheduler_state(
        self,
        ctx: IngressContext,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
    ) -> None: ...

    async def begin_interaction_execution(self, ctx: IngressContext) -> bool: ...

    async def begin_interaction_recovery_execution(
        self, ctx: IngressContext
    ) -> bool: ...

    async def replay_interaction_delivery(self, ctx: IngressContext) -> None: ...

    async def finish_interaction_execution(
        self,
        ctx: IngressContext,
        *,
        execution_status: str,
        execution_error: Optional[str] = None,
    ) -> None: ...

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None: ...

    async def send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None: ...


@dataclass(frozen=True)
class InteractionSchedule:
    resource_keys: tuple[str, ...] = ()
    conversation_id: Optional[str] = None


@dataclass
class ScheduledInteraction:
    ctx: IngressContext
    payload: dict[str, Any]
    schedule: InteractionSchedule
    replay_mode: str = "normal"
    queue_wait_ack_policy: Optional[DiscordAckPolicy] = None
    submission_order: Optional[int] = None
    admitted: bool = False
    ready: Optional[asyncio.Future[None]] = None
    queue_wait_notice: Optional["QueueWaitNotice"] = None


@dataclass(frozen=True)
class QueueWaitNotice:
    scope: str
    command_label: str
    same_conversation: bool


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: Optional[float] = DEFAULT_HANDLER_TIMEOUT_SECONDS
    stalled_warning_seconds: Optional[float] = DEFAULT_STALLED_WARNING_SECONDS
    # Keep a finite cap so a burst of interactions cannot fan out into
    # unbounded handler concurrency and starve the event loop.
    max_concurrent_interaction_handlers: int = 4


class CommandRunner:
    def __init__(
        self,
        service: DiscordRunnerService,
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
        if config.max_concurrent_interaction_handlers < 1:
            raise ValueError("max_concurrent_interaction_handlers must be at least 1")
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._submission_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._drain_task: Optional[asyncio.Task[None]] = None
        self._submission_task: Optional[asyncio.Task[None]] = None
        self._interaction_tasks: Set[asyncio.Task[None]] = set()
        self._pending_interactions: Deque[ScheduledInteraction] = deque()
        self._pending_ordered_submissions: dict[int, ScheduledInteraction] = {}
        self._skipped_submission_orders: set[int] = set()
        self._next_submission_order = 1
        self._active_resource_keys: set[str] = set()
        self._active_resource_owners: dict[str, ScheduledInteraction] = {}
        self._active_conversation_labels: dict[str, str] = {}
        self._interaction_handler_slots = asyncio.Semaphore(
            config.max_concurrent_interaction_handlers
        )
        self._scheduler_lock = asyncio.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(
                self._drain_loop(), name="discord-runner-drain"
            )
        if self._submission_task is None or self._submission_task.done():
            self._submission_task = asyncio.create_task(
                self._submission_loop(), name="discord-runner-submissions"
            )

    @property
    def active_task_count(self) -> int:
        self._interaction_tasks = {
            task for task in self._interaction_tasks if not task.done()
        }
        return len(self._interaction_tasks)

    def submit(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
        *,
        resource_keys: Sequence[str] = (),
        conversation_id: Optional[str] = None,
        queue_wait_ack_policy: Optional[DiscordAckPolicy] = None,
        submission_order: Optional[int] = None,
    ) -> None:
        self._ensure_submission_started()
        schedule = InteractionSchedule(
            resource_keys=tuple(dict.fromkeys(resource_keys)),
            conversation_id=conversation_id,
        )
        item = ScheduledInteraction(
            ctx=ctx,
            payload=payload,
            schedule=schedule,
            replay_mode="normal",
            queue_wait_ack_policy=queue_wait_ack_policy,
            submission_order=submission_order,
        )
        self._submission_queue.put_nowait(item)

    def submit_recovery(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
        *,
        resource_keys: Sequence[str] = (),
        conversation_id: Optional[str] = None,
        replay_mode: str,
    ) -> None:
        self._ensure_submission_started()
        schedule = InteractionSchedule(
            resource_keys=tuple(dict.fromkeys(resource_keys)),
            conversation_id=conversation_id,
        )
        item = ScheduledInteraction(
            ctx=ctx,
            payload=payload,
            schedule=schedule,
            replay_mode=replay_mode,
            queue_wait_ack_policy=None,
        )
        self._submission_queue.put_nowait(item)

    def submit_event(self, event: Any) -> None:
        self._ensure_started()
        self._queue.put_nowait(event)

    def skip_submission_order(self, submission_order: Optional[int]) -> None:
        if submission_order is None or submission_order < self._next_submission_order:
            return
        self._ensure_submission_started()
        self._skipped_submission_orders.add(submission_order)
        for scheduled in self._consume_ready_submission_items():
            task = asyncio.create_task(
                self._run_scheduled_interaction(scheduled),
                name=f"discord-runner-{scheduled.ctx.interaction_id}",
            )
            self._track_interaction_task(task)

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

    async def _mark_scheduler_state(
        self,
        item: ScheduledInteraction,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
    ) -> None:
        await self._service.mark_interaction_scheduler_state(
            item.ctx,
            scheduler_state=scheduler_state,
            increment_attempt_count=increment_attempt_count,
        )

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        idle_conversations: set[str] = set()
        deadline = time.monotonic() + grace_seconds
        if self._drain_task is not None and not self._drain_task.done():
            await self._queue.put(_DRAIN_SENTINEL)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._drain_task),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                self._drain_task.cancel()
                with contextlib_suppress(asyncio.CancelledError):
                    await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        if self._submission_task is not None and not self._submission_task.done():
            await self._submission_queue.put(_SUBMISSION_SENTINEL)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._submission_task),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                self._submission_task.cancel()
                with contextlib_suppress(asyncio.CancelledError):
                    await self._submission_task
            except asyncio.CancelledError:
                pass
            self._submission_task = None
        if self._interaction_tasks:
            idle_conversations.update(self._active_conversation_labels)
            idle_conversations.update(
                item.schedule.conversation_id
                for item in self._pending_interactions
                if item.schedule.conversation_id
            )
            await self._drain_interaction_tasks(deadline=deadline)
        async with self._scheduler_lock:
            self._pending_interactions.clear()
            self._pending_ordered_submissions.clear()
            self._skipped_submission_orders.clear()
            self._next_submission_order = 1
            self._active_resource_keys.clear()
            self._active_resource_owners.clear()
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

    def _ensure_submission_started(self) -> None:
        if self._submission_task is None or self._submission_task.done():
            self._submission_task = asyncio.create_task(
                self._submission_loop(), name="discord-runner-submissions"
            )

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
                    await self._service.dispatch_chat_event(item)
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

    async def _submission_loop(self) -> None:
        try:
            while True:
                item = await self._submission_queue.get()
                if item is _SUBMISSION_SENTINEL:
                    self._submission_queue.task_done()
                    return
                try:
                    for scheduled in self._pop_ready_submission_items(item):
                        task = asyncio.create_task(
                            self._run_scheduled_interaction(scheduled),
                            name=f"discord-runner-{scheduled.ctx.interaction_id}",
                        )
                        self._track_interaction_task(task)
                finally:
                    self._submission_queue.task_done()
        except asyncio.CancelledError:
            return

    def _pop_ready_submission_items(
        self, item: ScheduledInteraction
    ) -> list[ScheduledInteraction]:
        order = item.submission_order
        if order is None:
            return [item]
        if order < self._next_submission_order:
            return [item]
        self._pending_ordered_submissions[order] = item
        return self._consume_ready_submission_items()

    def _consume_ready_submission_items(self) -> list[ScheduledInteraction]:
        ready: list[ScheduledInteraction] = []
        while True:
            if self._next_submission_order in self._skipped_submission_orders:
                self._skipped_submission_orders.remove(self._next_submission_order)
                self._next_submission_order += 1
                continue
            if self._next_submission_order not in self._pending_ordered_submissions:
                break
            ready.append(
                self._pending_ordered_submissions.pop(self._next_submission_order)
            )
            self._next_submission_order += 1
        return ready

    async def _run_scheduled_interaction(self, item: ScheduledInteraction) -> None:
        acquired_schedule = False
        queue_wait_ack_attempted = False
        notify_idle: set[str] = set()
        try:
            if item.schedule.resource_keys:
                admitted_immediately = await self._admit_or_enqueue_schedule(item)
                if admitted_immediately:
                    await self._mark_scheduler_state(
                        item,
                        scheduler_state="scheduled",
                    )
                    acquired_schedule = True
                else:
                    await self._mark_scheduler_state(
                        item,
                        scheduler_state="waiting_on_resources",
                    )
                    if item.queue_wait_ack_policy not in (None, "immediate"):
                        queue_wait_ack_attempted = True
                        acknowledged = await self._service.acknowledge_runtime_envelope(
                            RuntimeInteractionEnvelope(
                                context=item.ctx,
                                conversation_id=item.schedule.conversation_id,
                                resource_keys=item.schedule.resource_keys,
                                queue_wait_ack_policy=item.queue_wait_ack_policy,
                            ),
                            stage="queue_wait",
                        )
                        if not acknowledged:
                            notify_idle = await self._remove_pending_schedule(item)
                            return
                    await self._maybe_send_queue_wait_notice(item)
                    await self._wait_for_schedule(item)
                    await self._mark_scheduler_state(
                        item,
                        scheduler_state="scheduled",
                    )
                    acquired_schedule = True
            else:
                await self._mark_scheduler_state(
                    item,
                    scheduler_state="scheduled",
                )
            if (
                not queue_wait_ack_attempted
                and item.queue_wait_ack_policy == "defer_ephemeral"
            ):
                # Queue-wait ACK must happen before waiting for a global handler
                # slot; otherwise unrelated handler saturation can push ACK past
                # Discord's callback window.
                acknowledged = await self._service.acknowledge_runtime_envelope(
                    RuntimeInteractionEnvelope(
                        context=item.ctx,
                        conversation_id=item.schedule.conversation_id,
                        resource_keys=item.schedule.resource_keys,
                        queue_wait_ack_policy=item.queue_wait_ack_policy,
                    ),
                    stage="queue_wait",
                )
                if not acknowledged:
                    return
            async with self._interaction_handler_slots:
                await self._run_with_lifecycle(
                    item.ctx,
                    item.payload,
                    replay_mode=item.replay_mode,
                )
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
            queue_wait_notice = self._find_queue_wait_notice_locked(item)
            if queue_wait_notice is None:
                self._admit_interaction_locked(item)
                return True
            item.ready = loop.create_future()
            item.queue_wait_notice = queue_wait_notice
            self._pending_interactions.append(item)
            return False

    @staticmethod
    def _resource_scope(resource_key: str) -> str:
        if resource_key.startswith("conversation:"):
            return "channel"
        if resource_key.startswith("workspace:"):
            return "workspace"
        return "resource"

    def _build_queue_wait_notice(
        self,
        *,
        resource_key: str,
        blocker: ScheduledInteraction,
        item: ScheduledInteraction,
    ) -> QueueWaitNotice:
        return QueueWaitNotice(
            scope=self._resource_scope(resource_key),
            command_label=self._format_command_label(blocker.ctx),
            same_conversation=(
                blocker.schedule.conversation_id == item.schedule.conversation_id
            ),
        )

    def _find_queue_wait_notice_locked(
        self, item: ScheduledInteraction
    ) -> Optional[QueueWaitNotice]:
        for resource_key in item.schedule.resource_keys:
            blocker = self._active_resource_owners.get(resource_key)
            if blocker is not None:
                return self._build_queue_wait_notice(
                    resource_key=resource_key,
                    blocker=blocker,
                    item=item,
                )
        for pending in self._pending_interactions:
            for resource_key in item.schedule.resource_keys:
                if resource_key not in pending.schedule.resource_keys:
                    continue
                return self._build_queue_wait_notice(
                    resource_key=resource_key,
                    blocker=pending,
                    item=item,
                )
        return None

    @staticmethod
    def _queue_wait_text(item: ScheduledInteraction) -> Optional[str]:
        notice = item.queue_wait_notice
        if notice is None:
            return None
        if notice.scope == "channel":
            return (
                f"Queued behind {notice.command_label} in this channel; "
                "will run when it finishes."
            )
        if notice.scope == "workspace":
            if notice.same_conversation:
                return (
                    f"Queued behind {notice.command_label} for this workspace; "
                    "will run when it finishes."
                )
            return (
                f"Queued behind {notice.command_label} in another channel bound "
                "to the same workspace; will run when it finishes."
            )
        return f"Queued behind {notice.command_label}; will run when it finishes."

    async def _maybe_send_queue_wait_notice(self, item: ScheduledInteraction) -> None:
        if item.replay_mode != "normal":
            return
        if item.ctx.kind != item.ctx.kind.SLASH_COMMAND or not item.ctx.deferred:
            return
        text = self._queue_wait_text(item)
        if not isinstance(text, str) or not text.strip():
            return
        ack_policy = item.ctx.command_spec.ack_policy if item.ctx.command_spec else None
        if ack_policy == "defer_public":
            await self._service.send_or_respond_public(
                interaction_id=item.ctx.interaction_id,
                interaction_token=item.ctx.interaction_token,
                deferred=True,
                text=text,
            )
            return
        await self._service.send_or_respond_ephemeral(
            interaction_id=item.ctx.interaction_id,
            interaction_token=item.ctx.interaction_token,
            deferred=True,
            text=text,
        )

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
        for resource_key in item.schedule.resource_keys:
            self._active_resource_owners[resource_key] = item
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
            if self._active_resource_owners.get(key) is item:
                self._active_resource_owners.pop(key, None)
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

    def _track_interaction_task(self, task: asyncio.Task[None]) -> None:
        self._interaction_tasks.add(task)
        task.add_done_callback(self._interaction_task_done)

    async def _drain_interaction_tasks(self, *, deadline: float) -> None:
        while self._interaction_tasks:
            tasks = list(self._interaction_tasks)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                continue
            await asyncio.wait(
                tasks,
                timeout=remaining,
                return_when=asyncio.ALL_COMPLETED,
            )

    async def _run_with_lifecycle(
        self,
        ctx: IngressContext,
        payload: dict[str, Any],
        *,
        replay_mode: str = "normal",
    ) -> None:
        if replay_mode == "delivery_replay":
            await self._service.replay_interaction_delivery(ctx)
            return
        claimed_execution = True
        if replay_mode == "execution_replay":
            claimed_execution = bool(
                await self._service.begin_interaction_recovery_execution(ctx)
            )
        else:
            claimed_execution = bool(
                await self._service.begin_interaction_execution(ctx)
            )
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
            await self._service.finish_interaction_execution(
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
        timeout_task = asyncio.create_task(
            self._send_timeout_followup(ctx),
            name=f"discord-runner-timeout-followup-{ctx.interaction_id}",
        )
        self._track_interaction_task(timeout_task)

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
            await self._service.send_or_respond_ephemeral(
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
            await self._service.send_or_respond_ephemeral(
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
