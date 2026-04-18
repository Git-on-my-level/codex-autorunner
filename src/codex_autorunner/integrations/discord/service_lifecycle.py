from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Optional, cast

from ...core.logging_utils import log_event
from ...core.managed_processes import (
    reap_managed_processes as _reap_managed_processes_core,
)
from ...core.utils import canonicalize_path

CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS = 2.0
_DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS = 10.0
DISCORD_INTERACTION_COLD_START_WINDOW_SECONDS = 120.0
DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS = 300.0
DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS = 600.0


def service_uptime_ms(service: Any, *, now: Optional[float] = None) -> Optional[float]:
    started_at_raw = getattr(service, "_service_started_at_monotonic", None)
    started_at = (
        float(started_at_raw) if isinstance(started_at_raw, (int, float)) else None
    )
    if started_at is None:
        return None
    current = time.monotonic() if now is None else now
    return round(max(0.0, (current - started_at) * 1000), 1)


def is_within_cold_start_window(service: Any, *, now: Optional[float] = None) -> bool:
    started_at_raw = getattr(service, "_service_started_at_monotonic", None)
    started_at = (
        float(started_at_raw) if isinstance(started_at_raw, (int, float)) else None
    )
    if started_at is None:
        return False
    current = time.monotonic() if now is None else now
    return current - started_at <= DISCORD_INTERACTION_COLD_START_WINDOW_SECONDS


def reap_managed_processes(service: Any, *, stage: str) -> None:
    try:
        cleanup = _reap_managed_processes_core(service._config.root)
        if cleanup.killed or cleanup.signaled or cleanup.removed:
            log_event(
                service._logger,
                logging.INFO,
                "discord.process_reaper.cleaned",
                stage=stage,
                killed=cleanup.killed,
                signaled=cleanup.signaled,
                removed=cleanup.removed,
                skipped=cleanup.skipped,
            )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.process_reaper.failed",
            stage=stage,
            exc=exc,
        )


def validate_command_sync_config(service: Any) -> None:
    registration = service._config.command_registration
    if not registration.enabled:
        return

    application_id = (service._config.application_id or "").strip()
    if not application_id:
        raise ValueError("missing Discord application id for command sync")
    if registration.scope == "guild" and not registration.guild_ids:
        raise ValueError("guild scope requires at least one guild_id")


async def sync_application_commands_on_startup(service: Any) -> None:
    from .command_registry import sync_commands
    from .errors import DiscordAPIError
    from .interaction_registry import build_application_commands

    registration = service._config.command_registration
    if not registration.enabled:
        log_event(
            service._logger,
            logging.INFO,
            "discord.commands.sync.disabled",
        )
        return

    validate_command_sync_config(service)

    application_id = (service._config.application_id or "").strip()
    commands = build_application_commands(service)
    try:
        await sync_commands(
            service._rest,
            application_id=application_id,
            commands=commands,
            scope=registration.scope,
            guild_ids=registration.guild_ids,
            logger=service._logger,
        )
    except ValueError:
        raise
    except (DiscordAPIError, OSError, RuntimeError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.commands.sync.startup_failed",
            scope=registration.scope,
            command_count=len(commands),
            exc=exc,
        )


async def reconcile_progress_leases_on_startup(service: Any) -> None:
    from .message_turns import reconcile_discord_turn_progress_leases

    try:
        reconciled = await reconcile_discord_turn_progress_leases(
            service,
            orphaned=True,
            startup=True,
        )
    except Exception as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.turn.progress_reconcile_startup_failed",
            exc=exc,
        )
        return
    if reconciled:
        log_event(
            service._logger,
            logging.INFO,
            "discord.turn.progress_reconcile_startup_finished",
            reconciled=reconciled,
        )


async def next_opencode_prune_interval_seconds(service: Any) -> float:
    async with service._opencode_lock:
        intervals = [
            entry.prune_interval_seconds
            for entry in service._opencode_supervisors.values()
            if entry.prune_interval_seconds is not None
        ]
        has_supervisors = bool(service._opencode_supervisors)
    if intervals:
        return cast(float, min(intervals))
    if not has_supervisors:
        return DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS
    return DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS


async def run_opencode_prune_loop(service: Any) -> None:
    while True:
        await asyncio.sleep(await next_opencode_prune_interval_seconds(service))
        await prune_opencode_supervisors(service)


async def prune_opencode_supervisors(service: Any) -> None:
    async with service._opencode_lock:
        cached_entries = list(service._opencode_supervisors.items())
    cached_supervisors = len(cached_entries)
    if not cached_entries:
        log_event(
            service._logger,
            logging.DEBUG,
            "discord.opencode.prune_sweep",
            cached_supervisors=0,
            cached_supervisors_after=0,
            live_handles=0,
            killed_processes=0,
            evicted_supervisors=0,
        )
        return

    now = time.monotonic()
    live_handles = 0
    killed_processes = 0
    eviction_candidates: list[tuple[str, Any]] = []

    for workspace_path, entry in cached_entries:
        workspace_root = canonicalize_path(Path(workspace_path))
        execution_running = service._workspace_has_running_opencode_execution(
            workspace_root
        )
        if execution_running is not False:
            entry.last_requested_at = now
            try:
                snapshot = await entry.supervisor.lifecycle_snapshot()
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.opencode.prune_failed",
                    workspace_path=workspace_path,
                    exc=exc,
                )
            else:
                live_handles += snapshot.cached_handles
            log_event(
                service._logger,
                logging.DEBUG,
                "discord.opencode.prune_deferred",
                workspace_path=workspace_path,
                reason=(
                    "active_runtime_execution"
                    if execution_running
                    else "execution_state_unknown"
                ),
            )
            continue
        try:
            killed_processes += await entry.supervisor.prune_idle()
            snapshot = await entry.supervisor.lifecycle_snapshot()
        except (OSError, RuntimeError, ValueError) as exc:
            log_event(
                service._logger,
                logging.WARNING,
                "discord.opencode.prune_failed",
                workspace_path=workspace_path,
                exc=exc,
            )
            continue
        live_handles += snapshot.cached_handles
        idle_for = max(0.0, now - entry.last_requested_at)
        eviction_delay = (
            entry.prune_interval_seconds
            or DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS
        )
        if snapshot.cached_handles == 0 and idle_for >= eviction_delay:
            eviction_candidates.append((workspace_path, entry))

    evicted_supervisors = 0
    evicted_objects: list[Any] = []
    if eviction_candidates:
        async with service._opencode_lock:
            for workspace_path, entry in eviction_candidates:
                current = service._opencode_supervisors.get(workspace_path)
                if current is not entry:
                    continue
                service._opencode_supervisors.pop(workspace_path, None)
                evicted_supervisors += 1
                evicted_objects.append(entry.supervisor)
    for supervisor in evicted_objects:
        with contextlib.suppress(Exception):
            await supervisor.close_all()

    async with service._opencode_lock:
        cached_supervisors_after = len(service._opencode_supervisors)
    log_event(
        service._logger,
        logging.DEBUG,
        "discord.opencode.prune_sweep",
        cached_supervisors=cached_supervisors,
        cached_supervisors_after=cached_supervisors_after,
        live_handles=live_handles,
        killed_processes=killed_processes,
        evicted_supervisors=evicted_supervisors,
    )


async def run_chat_queue_reset_loop(service: Any) -> None:
    while True:
        try:
            await apply_pending_chat_queue_resets(service)
        except Exception as exc:
            log_event(
                service._logger,
                logging.WARNING,
                "discord.chat_queue.reset_scan_failed",
                exc=exc,
            )
        await asyncio.sleep(CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS)


async def apply_pending_chat_queue_resets(service: Any) -> None:
    requests = service._chat_queue_control_store.take_reset_requests(platform="discord")
    for request in requests:
        conversation_id = str(request.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        result = await service._dispatcher.force_reset(conversation_id)
        log_event(
            service._logger,
            logging.WARNING,
            "discord.chat_queue.reset_applied",
            conversation_id=conversation_id,
            chat_id=request.get("chat_id"),
            thread_id=request.get("thread_id"),
            requested_at=request.get("requested_at"),
            requested_by=request.get("requested_by"),
            cancelled_pending=result.get("cancelled_pending"),
            cancelled_active=result.get("cancelled_active"),
        )


async def reconcile_background_task_failure(
    service: Any,
    task_context: dict[str, Any],
    *,
    allow_channel_fallback: bool = True,
) -> int:
    from .message_turns import (
        reconcile_discord_turn_progress_leases,
    )

    failure_note = task_context.get("failure_note")
    if not isinstance(failure_note, str) or not failure_note.strip():
        failure_note = "Status: this progress message lost its worker."
    reconciled = await reconcile_discord_turn_progress_leases(
        service,
        lease_id=(
            task_context.get("lease_id")
            if isinstance(task_context.get("lease_id"), str)
            else None
        ),
        managed_thread_id=(
            task_context.get("managed_thread_id")
            if isinstance(task_context.get("managed_thread_id"), str)
            else None
        ),
        execution_id=(
            task_context.get("execution_id")
            if isinstance(task_context.get("execution_id"), str)
            else None
        ),
        channel_id=(
            task_context.get("channel_id")
            if isinstance(task_context.get("channel_id"), str)
            else None
        ),
        message_id=(
            task_context.get("message_id")
            if isinstance(task_context.get("message_id"), str)
            else None
        ),
        failure_note=failure_note,
        orphaned=bool(task_context.get("orphaned")),
    )
    if reconciled:
        return int(reconciled)
    if not allow_channel_fallback:
        return 0
    fallback_channel_id = (
        task_context.get("channel_id")
        if isinstance(task_context.get("channel_id"), str)
        else None
    )
    if fallback_channel_id:
        await service._send_channel_message_safe(
            fallback_channel_id,
            {"content": failure_note},
        )
    return 0


def on_background_task_done(service: Any, task: asyncio.Task[Any]) -> None:
    from .message_turns import bind_discord_progress_task_context

    service._background_tasks.discard(task)
    service._background_shutdown_wait_tasks.discard(task)
    task_context = getattr(task, "_discord_progress_task_context", None)
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.background_task.failed",
            exc=exc,
        )
        if isinstance(task_context, dict) and task_context:

            async def _reconcile_failure() -> None:
                await reconcile_background_task_failure(service, task_context)

            reconcile_task = service._spawn_task(_reconcile_failure())
            bind_discord_progress_task_context(reconcile_task)


async def close_all_app_server_supervisors(service: Any) -> None:
    async with service._app_server_lock:
        supervisors = list(service._app_server_supervisors.values())
        service._app_server_supervisors.clear()
    for supervisor in supervisors:
        with contextlib.suppress(Exception):
            await supervisor.close_all()


async def close_all_opencode_supervisors(service: Any) -> None:
    async with service._opencode_lock:
        opencode_supervisors = [
            entry.supervisor for entry in service._opencode_supervisors.values()
        ]
        service._opencode_supervisors.clear()
    for supervisor in opencode_supervisors:
        with contextlib.suppress(Exception):
            await supervisor.close_all()


async def shutdown_service(service: Any) -> None:

    shutdown_deadline = (
        time.monotonic() + _DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS
    )
    pending_shutdown_tasks: list[asyncio.Task[Any]] = []
    while True:
        drainable_tasks = [
            task
            for task in list(service._background_shutdown_wait_tasks)
            if not task.done()
        ]
        if not drainable_tasks:
            break
        remaining = shutdown_deadline - time.monotonic()
        if remaining <= 0:
            pending_shutdown_tasks = drainable_tasks
            break
        done, pending = await asyncio.wait(
            drainable_tasks,
            timeout=remaining,
        )
        service._background_shutdown_wait_tasks.difference_update(done)
        pending_shutdown_tasks = list(pending)
        if pending:
            break
    if pending_shutdown_tasks:
        shutdown_reconcile_contexts: list[dict[str, Any]] = []
        for task in pending_shutdown_tasks:
            task_context = getattr(task, "_discord_progress_task_context", None)
            if not isinstance(task_context, dict) or not task_context:
                continue
            shutdown_context = dict(task_context)
            shutdown_note = shutdown_context.get("shutdown_note")
            if isinstance(shutdown_note, str) and shutdown_note.strip():
                shutdown_context["failure_note"] = shutdown_note.strip()
            shutdown_reconcile_contexts.append(shutdown_context)
        if shutdown_reconcile_contexts:
            await asyncio.gather(
                *(
                    reconcile_background_task_failure(
                        service,
                        task_context,
                        allow_channel_fallback=False,
                    )
                    for task_context in shutdown_reconcile_contexts
                ),
                return_exceptions=True,
            )
            log_event(
                service._logger,
                logging.WARNING,
                "discord.background_task.shutdown_timeout",
                timeout_seconds=_DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS,
                pending_count=len(pending_shutdown_tasks),
            )
    if service._background_tasks:
        for task in list(service._background_tasks):
            task.cancel()
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)
        service._background_tasks.clear()
    service._background_shutdown_wait_tasks.clear()
    await service._command_runner.shutdown()
    if service._owns_gateway:
        with contextlib.suppress(Exception):
            await service._gateway.stop()
    if service._owns_rest and hasattr(service._rest, "close"):
        with contextlib.suppress(Exception):
            await service._rest.close()
    if service._owns_store:
        with contextlib.suppress(Exception):
            await service._store.close()
    await close_all_app_server_supervisors(service)
    await close_all_opencode_supervisors(service)
    if service._hub_client is not None:
        with contextlib.suppress(Exception):
            await service._hub_client.aclose()
    service._reap_managed_processes(stage="shutdown")
