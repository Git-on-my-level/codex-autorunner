from __future__ import annotations

from pathlib import Path
from typing import Any

from ...integrations.chat.bound_live_progress import (
    build_bound_chat_queue_execution_controller,
    resolve_bound_chat_queue_progress_context,
)
from ...integrations.chat.managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as recover_surface_managed_thread_executions_on_startup,
)
from ...integrations.chat.managed_thread_turns import ManagedThreadExecutionHooks
from .managed_thread_delivery import build_discord_managed_thread_durable_delivery_hooks
from .managed_thread_routing import (
    _build_discord_managed_thread_coordinator,
    _build_discord_runner_hooks,
)
from .message_turns import build_discord_thread_orchestration_service
from .progress_leases import (
    _get_discord_thread_queue_task_map,
    _spawn_discord_progress_background_task,
    cleanup_discord_terminal_progress_leases,
)


async def recover_managed_thread_executions_on_startup(service: Any) -> None:
    public_execution_error = "Discord PMA turn failed"

    def _build_execution_hooks(
        owner: Any,
        surface_key: str,
        managed_thread_id: str,
        _thread: Any,
    ) -> Any:
        hub_root, raw_config = resolve_bound_chat_queue_progress_context(
            owner,
            fallback_root=Path(owner._config.root),
        )

        async def _cleanup_interrupted_progress(
            _started: Any,
            finalized: Any,
        ) -> None:
            status = str(getattr(finalized, "status", "") or "").strip().lower()
            managed_turn_id = str(
                getattr(finalized, "managed_turn_id", "") or ""
            ).strip()
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

    def _recover_pending_queue(
        owner: Any,
        orchestration_service: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
    ) -> bool:
        workspace_root = getattr(thread, "workspace_root", None)
        if not workspace_root:
            return False
        coordinator = _build_discord_managed_thread_coordinator(
            service=owner,
            orchestration_service=orchestration_service,
            channel_id=surface_key,
            public_execution_error=public_execution_error,
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            pma_enabled=True,
        )
        queue_worker_hooks = _build_discord_runner_hooks(
            owner,
            channel_id=surface_key,
            managed_thread_id=managed_thread_id,
            workspace_root=Path(str(workspace_root)),
            public_execution_error=public_execution_error,
        ).queue_worker_hooks()
        coordinator.ensure_queue_worker(
            task_map=_get_discord_thread_queue_task_map(owner),
            managed_thread_id=managed_thread_id,
            spawn_task=lambda coro: _spawn_discord_progress_background_task(
                owner,
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
        recover_pending_queue=_recover_pending_queue,
        public_execution_error=public_execution_error,
        failure_event_name="discord.turn.startup_execution_recovery_failed",
        finished_event_name="discord.turn.startup_execution_recovery_finished",
    )
