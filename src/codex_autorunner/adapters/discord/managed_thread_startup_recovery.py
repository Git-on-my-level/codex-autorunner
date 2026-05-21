from __future__ import annotations

from pathlib import Path
from typing import Any

from ...adapters.chat.bound_live_progress import (
    build_bound_chat_queue_execution_controller,
    resolve_bound_chat_queue_progress_context,
)
from ...adapters.chat.managed_thread_startup_recovery import (
    ManagedThreadStartupReattachResult,
    build_reattached_runtime_thread_execution,
    start_reattached_managed_thread_delivery_task,
)
from ...adapters.chat.managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as recover_surface_managed_thread_executions_on_startup,
)
from ...adapters.chat.managed_thread_turns import ManagedThreadExecutionHooks
from .managed_thread_delivery import build_discord_managed_thread_durable_delivery_hooks
from .managed_thread_routing import (
    _build_discord_managed_thread_coordinator,
    _build_discord_runner_hooks,
    build_discord_thread_orchestration_service,
)
from .progress_leases import (
    _get_discord_thread_queue_task_map,
    _spawn_discord_progress_background_task,
    cleanup_discord_terminal_progress_leases,
)


def _build_discord_startup_recovery_execution_hooks(
    owner: Any,
    *,
    surface_key: str,
    managed_thread_id: str,
) -> ManagedThreadExecutionHooks:
    hub_root, raw_config = resolve_bound_chat_queue_progress_context(
        owner,
        fallback_root=Path(owner._config.root),
    )

    async def _cleanup_interrupted_progress(
        _started: Any,
        finalized: Any,
    ) -> None:
        status = str(getattr(finalized, "status", "") or "").strip().lower()
        managed_turn_id = str(getattr(finalized, "managed_turn_id", "") or "").strip()
        if status != "interrupted" or not managed_turn_id:
            return
        await cleanup_discord_terminal_progress_leases(
            owner,
            managed_thread_id=managed_thread_id,
            execution_id=managed_turn_id,
            channel_id=surface_key,
            note="Status: this turn was interrupted.",
            record_prefix=(
                "discord:startup-recovery-interrupted-progress-cleanup:"
                f"{managed_thread_id}:{managed_turn_id}"
            ),
        )

    return build_bound_chat_queue_execution_controller(
        hub_root=hub_root,
        raw_config=raw_config,
        managed_thread_id=managed_thread_id,
        surface_targets=(("discord", surface_key),),
        base_hooks=ManagedThreadExecutionHooks(
            on_execution_finalized=_cleanup_interrupted_progress
        ),
    ).hooks


def recover_pending_discord_managed_thread_queue(
    service: Any,
    *,
    orchestration_service: Any,
    surface_key: str,
    managed_thread_id: str,
    thread: Any,
    public_execution_error: str = "Discord PMA turn failed",
) -> bool:
    workspace_root = getattr(thread, "workspace_root", None)
    if not workspace_root:
        return False
    coordinator = _build_discord_managed_thread_coordinator(
        service=service,
        orchestration_service=orchestration_service,
        channel_id=surface_key,
        public_execution_error=public_execution_error,
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        pma_enabled=True,
    )
    queue_worker_hooks = _build_discord_runner_hooks(
        service,
        channel_id=surface_key,
        managed_thread_id=managed_thread_id,
        workspace_root=Path(str(workspace_root)),
        public_execution_error=public_execution_error,
    ).queue_worker_hooks()
    coordinator.ensure_queue_worker(
        task_map=_get_discord_thread_queue_task_map(service),
        managed_thread_id=managed_thread_id,
        spawn_task=lambda coro: _spawn_discord_progress_background_task(
            service,
            coro,
            managed_thread_id=managed_thread_id,
            failure_note=(
                "Status: this progress message lost its queue worker and is "
                "no longer live. Please retry if needed."
            ),
            orphaned=True,
            reconcile_on_cancel=True,
            await_on_shutdown=True,
        ),
        hooks=queue_worker_hooks,
    )
    return True


def reattach_running_discord_managed_thread_execution(
    service: Any,
    *,
    orchestration_service: Any,
    surface_key: str,
    managed_thread_id: str,
    thread: Any,
    execution: Any,
    public_execution_error: str = "Discord PMA turn failed",
) -> ManagedThreadStartupReattachResult:
    started = build_reattached_runtime_thread_execution(
        orchestration_service=orchestration_service,
        managed_thread_id=managed_thread_id,
        thread=thread,
        execution=execution,
    )
    if started is None:
        workspace_root_raw = getattr(thread, "workspace_root", None)
        backend_thread_id = str(getattr(thread, "backend_thread_id", "") or "").strip()
        backend_turn_id = str(getattr(execution, "backend_id", "") or "").strip()
        if not workspace_root_raw or not backend_thread_id or not backend_turn_id:
            return ManagedThreadStartupReattachResult("missing_backend_binding")
        return ManagedThreadStartupReattachResult("missing_harness_or_unsupported")
    workspace_root = started.workspace_root
    coordinator = _build_discord_managed_thread_coordinator(
        service=service,
        orchestration_service=orchestration_service,
        channel_id=surface_key,
        public_execution_error=public_execution_error,
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        pma_enabled=True,
    )
    runner_hooks = _build_discord_runner_hooks(
        service,
        channel_id=surface_key,
        managed_thread_id=managed_thread_id,
        workspace_root=workspace_root,
        public_execution_error=public_execution_error,
    ).queue_worker_hooks()
    task_map = _get_discord_thread_queue_task_map(service)
    return start_reattached_managed_thread_delivery_task(
        service=service,
        managed_thread_id=managed_thread_id,
        started=started,
        coordinator=coordinator,
        runner_hooks=runner_hooks,
        startup_hooks=_build_discord_startup_recovery_execution_hooks(
            service,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
        ),
        task_map=task_map,
        spawn_task=lambda coro: _spawn_discord_progress_background_task(
            service,
            coro,
            managed_thread_id=managed_thread_id,
            execution_id=str(getattr(execution, "execution_id", "") or "").strip()
            or None,
            channel_id=surface_key,
            failure_note=(
                "Status: this recovered Discord worker failed and is no longer live. "
                "Please retry if needed."
            ),
            orphaned=True,
            reconcile_on_cancel=True,
            await_on_shutdown=True,
        ),
        rearm_queue_worker=lambda: coordinator.ensure_queue_worker(
            task_map=task_map,
            managed_thread_id=managed_thread_id,
            spawn_task=lambda coro: _spawn_discord_progress_background_task(
                service,
                coro,
                managed_thread_id=managed_thread_id,
                failure_note=(
                    "Status: this progress message lost its queue worker and is "
                    "no longer live. Please retry if needed."
                ),
                orphaned=True,
                reconcile_on_cancel=True,
                await_on_shutdown=True,
            ),
            hooks=runner_hooks,
        ),
    )


async def recover_managed_thread_executions_on_startup(service: Any) -> None:
    public_execution_error = "Discord PMA turn failed"

    def _build_execution_hooks(
        owner: Any,
        surface_key: str,
        managed_thread_id: str,
        _thread: Any,
    ) -> Any:
        return _build_discord_startup_recovery_execution_hooks(
            owner,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
        )

    def _recover_pending_queue(
        owner: Any,
        orchestration_service: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
    ) -> bool:
        return recover_pending_discord_managed_thread_queue(
            owner,
            orchestration_service=orchestration_service,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            thread=thread,
            public_execution_error=public_execution_error,
        )

    def _reattach_running_execution(
        owner: Any,
        orchestration_service: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
        execution: Any,
    ) -> ManagedThreadStartupReattachResult:
        return reattach_running_discord_managed_thread_execution(
            owner,
            orchestration_service=orchestration_service,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            thread=thread,
            execution=execution,
            public_execution_error=public_execution_error,
        )

    await recover_surface_managed_thread_executions_on_startup(
        service,
        surface_kind="discord",
        build_orchestration_service=build_discord_thread_orchestration_service,
        build_durable_delivery=lambda owner, surface_key, managed_thread_id, thread, public_execution_error: (
            None
            if not getattr(thread, "workspace_root", None)
            else build_discord_managed_thread_durable_delivery_hooks(
                owner,
                channel_id=surface_key,
                managed_thread_id=managed_thread_id,
                workspace_root=Path(str(thread.workspace_root)),
                public_execution_error=public_execution_error,
            )
        ),
        build_execution_hooks=_build_execution_hooks,
        reattach_running_execution=_reattach_running_execution,
        recover_pending_queue=_recover_pending_queue,
        public_execution_error=public_execution_error,
        failure_event_name="discord.turn.startup_execution_recovery_failed",
        finished_event_name="discord.turn.startup_execution_recovery_finished",
    )
