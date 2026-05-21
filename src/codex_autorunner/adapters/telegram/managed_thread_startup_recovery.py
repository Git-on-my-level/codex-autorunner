from __future__ import annotations

import asyncio
from typing import Any

from ..chat.bound_live_progress import (
    build_bound_chat_queue_execution_controller,
    resolve_bound_chat_queue_progress_context,
)
from ..chat.managed_thread_startup_recovery import (
    ManagedThreadStartupReattachResult,
    build_reattached_runtime_thread_execution,
    start_reattached_managed_thread_delivery_task,
)
from ..chat.managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as recover_surface_managed_thread_executions_on_startup,
)
from ..chat.managed_thread_turns import ManagedThreadExecutionHooks
from .handlers.commands.execution import (
    _build_telegram_managed_thread_coordinator,
    _build_telegram_runner_hooks,
    _build_telegram_thread_orchestration_service,
    _spawn_telegram_background_task,
)
from .state import parse_topic_key


def _build_telegram_startup_recovery_execution_hooks(
    owner: Any,
    *,
    surface_key: str,
    managed_thread_id: str,
    thread: Any,
) -> ManagedThreadExecutionHooks:
    hub_root, raw_config = resolve_bound_chat_queue_progress_context(
        owner,
        fallback_root=getattr(thread, "workspace_root", None) or owner._config.root,
    )
    return build_bound_chat_queue_execution_controller(
        hub_root=hub_root,
        raw_config=raw_config,
        managed_thread_id=managed_thread_id,
        surface_targets=(("telegram", surface_key),),
    ).hooks


def _telegram_queue_task_map(owner: Any) -> dict[str, asyncio.Task[Any]]:
    queue_task_map = getattr(owner, "_telegram_managed_thread_queue_tasks", None)
    if not isinstance(queue_task_map, dict):
        queue_task_map = {}
        owner._telegram_managed_thread_queue_tasks = queue_task_map
    return queue_task_map


def reattach_running_telegram_managed_thread_execution(
    service: Any,
    *,
    orchestration_service: Any,
    surface_key: str,
    managed_thread_id: str,
    thread: Any,
    execution: Any,
    public_execution_error: str = "Telegram PMA turn failed",
) -> ManagedThreadStartupReattachResult:
    workspace_root_raw = getattr(thread, "workspace_root", None)
    backend_thread_id = str(getattr(thread, "backend_thread_id", "") or "").strip()
    backend_turn_id = str(getattr(execution, "backend_id", "") or "").strip()
    if not workspace_root_raw or not backend_thread_id or not backend_turn_id:
        return ManagedThreadStartupReattachResult("missing_backend_binding")

    harness_for_thread = getattr(orchestration_service, "_harness_for_thread", None)
    if not callable(harness_for_thread):
        return ManagedThreadStartupReattachResult("missing_harness_or_unsupported")

    started = build_reattached_runtime_thread_execution(
        orchestration_service=orchestration_service,
        managed_thread_id=managed_thread_id,
        thread=thread,
        execution=execution,
    )
    if started is None:
        return ManagedThreadStartupReattachResult("missing_harness_or_unsupported")

    chat_id, thread_id, _ = parse_topic_key(surface_key)
    coordinator = _build_telegram_managed_thread_coordinator(
        service,
        orchestration_service=orchestration_service,
        surface_key=surface_key,
        chat_id=chat_id,
        thread_id=thread_id,
        public_execution_error=public_execution_error,
        timeout_error="Telegram PMA turn timed out",
        interrupted_error="Telegram PMA turn interrupted",
        pma_enabled=True,
    )
    runner_hooks = _build_telegram_runner_hooks(
        service,
        managed_thread_id=managed_thread_id,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_key=surface_key,
        public_execution_error=public_execution_error,
        workspace_path=workspace_root_raw,
        pma_enabled=True,
    ).queue_worker_hooks()
    task_map = _telegram_queue_task_map(service)
    return start_reattached_managed_thread_delivery_task(
        service=service,
        managed_thread_id=managed_thread_id,
        started=started,
        coordinator=coordinator,
        runner_hooks=runner_hooks,
        startup_hooks=_build_telegram_startup_recovery_execution_hooks(
            service,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            thread=thread,
        ),
        task_map=task_map,
        spawn_task=lambda coro: _spawn_telegram_background_task(service, coro),
        rearm_queue_worker=lambda: coordinator.ensure_queue_worker(
            task_map=task_map,
            managed_thread_id=managed_thread_id,
            spawn_task=lambda coro: _spawn_telegram_background_task(service, coro),
            hooks=runner_hooks,
        ),
    )


async def recover_managed_thread_executions_on_startup(service: Any) -> None:
    public_execution_error = "Telegram PMA turn failed"

    def _build_execution_hooks(
        owner: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
    ) -> Any:
        return _build_telegram_startup_recovery_execution_hooks(
            owner,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            thread=thread,
        )

    def _build_delivery(
        owner: Any,
        surface_key: str,
        _managed_thread_id: str,
        thread: Any,
        public_execution_error: str,
    ) -> Any:
        chat_id, thread_id, _ = parse_topic_key(surface_key)
        hooks = _build_telegram_runner_hooks(
            owner,
            managed_thread_id=_managed_thread_id,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_key=surface_key,
            public_execution_error=public_execution_error,
            workspace_path=getattr(thread, "workspace_root", None),
            pma_enabled=True,
        )
        return hooks.durable_delivery

    def _recover_pending_queue(
        owner: Any,
        orchestration_service: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
    ) -> bool:
        chat_id, thread_id, _ = parse_topic_key(surface_key)
        queue_task_map = _telegram_queue_task_map(owner)
        coordinator = _build_telegram_managed_thread_coordinator(
            owner,
            orchestration_service=orchestration_service,
            surface_key=surface_key,
            chat_id=chat_id,
            thread_id=thread_id,
            public_execution_error=public_execution_error,
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
            pma_enabled=True,
        )
        runner_hooks = _build_telegram_runner_hooks(
            owner,
            managed_thread_id=managed_thread_id,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_key=surface_key,
            public_execution_error=public_execution_error,
            workspace_path=getattr(thread, "workspace_root", None),
            pma_enabled=True,
        )
        coordinator.ensure_queue_worker(
            task_map=queue_task_map,
            managed_thread_id=managed_thread_id,
            spawn_task=lambda coro: _spawn_telegram_background_task(owner, coro),
            hooks=runner_hooks,
        )
        return True

    def _reattach_running_execution(
        owner: Any,
        orchestration_service: Any,
        surface_key: str,
        managed_thread_id: str,
        thread: Any,
        execution: Any,
    ) -> ManagedThreadStartupReattachResult:
        return reattach_running_telegram_managed_thread_execution(
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
        surface_kind="telegram",
        build_orchestration_service=_build_telegram_thread_orchestration_service,
        build_durable_delivery=_build_delivery,
        build_execution_hooks=_build_execution_hooks,
        reattach_running_execution=_reattach_running_execution,
        recover_pending_queue=_recover_pending_queue,
        public_execution_error=public_execution_error,
        failure_event_name="telegram.turn.startup_execution_recovery_failed",
        finished_event_name="telegram.turn.startup_execution_recovery_finished",
    )
