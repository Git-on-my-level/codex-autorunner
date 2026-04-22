from __future__ import annotations

from typing import Any

from ..chat.managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as recover_surface_managed_thread_executions_on_startup,
)
from .handlers.commands.execution import (
    _build_telegram_managed_thread_coordinator,
    _build_telegram_runner_hooks,
    _build_telegram_thread_orchestration_service,
    _spawn_telegram_background_task,
)
from .state import parse_topic_key


async def recover_managed_thread_executions_on_startup(service: Any) -> None:
    public_execution_error = "Telegram PMA turn failed"

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
        queue_task_map = getattr(owner, "_telegram_managed_thread_queue_tasks", None)
        if not isinstance(queue_task_map, dict):
            queue_task_map = {}
            owner._telegram_managed_thread_queue_tasks = queue_task_map
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

    await recover_surface_managed_thread_executions_on_startup(
        service,
        surface_kind="telegram",
        build_orchestration_service=_build_telegram_thread_orchestration_service,
        build_durable_delivery=_build_delivery,
        recover_pending_queue=_recover_pending_queue,
        public_execution_error=public_execution_error,
        failure_event_name="telegram.turn.startup_execution_recovery_failed",
        finished_event_name="telegram.turn.startup_execution_recovery_finished",
    )
