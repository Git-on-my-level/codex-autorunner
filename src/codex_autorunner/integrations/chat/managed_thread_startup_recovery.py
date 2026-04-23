from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Callable, Optional

from ...core.logging_utils import log_event
from .managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadExecutionHooks,
    ManagedThreadFinalizationResult,
    _invoke_execution_error_hook,
    _invoke_finalization_hook,
    _invoke_lifecycle_hook,
    handoff_managed_thread_final_delivery,
)

BuildOrchestrationService = Callable[[Any], Any]
BuildDurableDelivery = Callable[
    [Any, str, str, Any, str],
    Optional[ManagedThreadDurableDeliveryHooks],
]
BuildRecoveryExecutionHooks = Callable[
    [Any, str, str, Any],
    Optional[ManagedThreadExecutionHooks],
]
RecoverPendingQueue = Callable[[Any, Any, str, str, Any], object]
_PENDING_QUEUE_SCAN_LIMIT = 10_000


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _surface_client_turn_prefix(*, surface_kind: str, surface_key: str) -> str:
    return f"{surface_kind}:{surface_key}:"


def _safe_list_bindings(binding_store: Any, **kwargs: Any) -> tuple[Any, ...]:
    list_bindings = getattr(binding_store, "list_bindings", None)
    if not callable(list_bindings):
        return ()
    try:
        return tuple(list_bindings(**kwargs) or ())
    except Exception:
        return ()


def surface_owns_running_execution(
    thread_store: Any,
    *,
    managed_thread_id: str,
    surface_kind: str,
    surface_key: str,
) -> bool:
    get_running_turn = getattr(thread_store, "get_running_turn", None)
    if not callable(get_running_turn):
        return False
    running_turn = get_running_turn(managed_thread_id)
    client_turn_id = _normalized_optional_text(
        running_turn.get("client_turn_id") if running_turn is not None else None
    )
    if client_turn_id is None:
        return False
    return client_turn_id.lower().startswith(
        _surface_client_turn_prefix(
            surface_kind=surface_kind,
            surface_key=surface_key,
        ).lower()
    )


def find_surface_key_for_running_execution(
    binding_store: Any,
    thread_store: Any,
    *,
    managed_thread_id: str,
    surface_kind: str,
    limit: int = 1000,
) -> Optional[str]:
    for binding in _safe_list_bindings(
        binding_store,
        thread_target_id=managed_thread_id,
        surface_kind=surface_kind,
        include_disabled=False,
        limit=limit,
    ):
        surface_key = _normalized_optional_text(getattr(binding, "surface_key", None))
        if surface_key is None:
            continue
        if surface_owns_running_execution(
            thread_store,
            managed_thread_id=managed_thread_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        ):
            return surface_key
    return None


def surface_owns_pending_queue(
    thread_store: Any,
    *,
    managed_thread_id: str,
    surface_kind: str,
    surface_key: str,
) -> bool:
    list_pending = getattr(thread_store, "list_pending_turn_queue_items", None)
    get_turn = getattr(thread_store, "get_turn", None)
    if not callable(list_pending) or not callable(get_turn):
        return False
    prefix = _surface_client_turn_prefix(
        surface_kind=surface_kind,
        surface_key=surface_key,
    ).lower()
    for item in list_pending(managed_thread_id, limit=_PENDING_QUEUE_SCAN_LIMIT) or ():
        managed_turn_id = _normalized_optional_text(item.get("managed_turn_id"))
        if managed_turn_id is None:
            continue
        turn = get_turn(managed_thread_id, managed_turn_id)
        client_turn_id = _normalized_optional_text(
            turn.get("client_turn_id") if turn is not None else None
        )
        if client_turn_id is not None and client_turn_id.lower().startswith(prefix):
            return True
    return False


def find_surface_keys_for_pending_queue(
    binding_store: Any,
    thread_store: Any,
    *,
    managed_thread_id: str,
    surface_kind: str,
    limit: int = 1000,
) -> tuple[str, ...]:
    owned_surface_keys: list[str] = []
    for binding in _safe_list_bindings(
        binding_store,
        thread_target_id=managed_thread_id,
        surface_kind=surface_kind,
        include_disabled=False,
        limit=limit,
    ):
        surface_key = _normalized_optional_text(getattr(binding, "surface_key", None))
        if surface_key is None:
            continue
        if surface_owns_pending_queue(
            thread_store,
            managed_thread_id=managed_thread_id,
            surface_kind=surface_kind,
            surface_key=surface_key,
        ):
            owned_surface_keys.append(surface_key)
    return tuple(dict.fromkeys(owned_surface_keys))


async def recover_managed_thread_executions_on_startup(
    service: Any,
    *,
    surface_kind: str,
    build_orchestration_service: BuildOrchestrationService,
    build_durable_delivery: BuildDurableDelivery,
    build_execution_hooks: Optional[BuildRecoveryExecutionHooks] = None,
    recover_pending_queue: Optional[RecoverPendingQueue] = None,
    public_execution_error: str,
    failure_event_name: str,
    finished_event_name: str,
) -> None:
    orchestration_service = build_orchestration_service(service)
    thread_store = getattr(orchestration_service, "thread_store", None)
    list_running = getattr(
        thread_store, "list_thread_ids_with_running_executions", None
    )
    if not callable(list_running):
        return

    recovered = 0
    failed = 0
    running_thread_ids = tuple(list_running(limit=None) or ())
    for managed_thread_id in running_thread_ids:
        try:
            thread = orchestration_service.get_thread_target(managed_thread_id)
            execution = orchestration_service.get_running_execution(managed_thread_id)
            if thread is None or execution is None:
                continue
            surface_key = find_surface_key_for_running_execution(
                orchestration_service,
                thread_store,
                managed_thread_id=managed_thread_id,
                surface_kind=surface_kind,
                limit=1000,
            )
            if surface_key is None:
                continue

            recovered_execution = (
                await orchestration_service.recover_running_execution_from_harness(
                    managed_thread_id,
                    default_error=public_execution_error,
                )
            )
            if recovered_execution is None:
                recovered_execution = (
                    orchestration_service.recover_running_execution_after_restart(
                        managed_thread_id
                    )
                )
            if recovered_execution is None:
                continue

            delivery = build_durable_delivery(
                service,
                surface_key,
                managed_thread_id,
                thread,
                public_execution_error,
            )
            if delivery is None:
                continue
            execution_hooks = (
                build_execution_hooks(
                    service,
                    surface_key,
                    managed_thread_id,
                    thread,
                )
                if build_execution_hooks is not None
                else None
            )
            started = _build_recovered_started_execution(
                managed_thread_id=managed_thread_id,
                managed_turn_id=recovered_execution.execution_id,
                thread=thread,
                recovered_execution=recovered_execution,
            )

            finalized = ManagedThreadFinalizationResult(
                status=recovered_execution.status,
                assistant_text=recovered_execution.output_text or "",
                error=recovered_execution.error,
                managed_thread_id=managed_thread_id,
                managed_turn_id=recovered_execution.execution_id,
                backend_thread_id=getattr(thread, "backend_thread_id", None),
                token_usage=None,
            )
            if execution_hooks is not None:
                await _invoke_lifecycle_hook(
                    execution_hooks.on_execution_started,
                    started,
                )
            try:
                await handoff_managed_thread_final_delivery(
                    finalized,
                    delivery=delivery,
                    logger=service._logger,
                )
                if execution_hooks is not None:
                    await _invoke_finalization_hook(
                        execution_hooks.on_execution_finalized,
                        started,
                        finalized,
                    )
            except BaseException as exc:
                if execution_hooks is not None:
                    await _invoke_execution_error_hook(
                        execution_hooks.on_execution_error,
                        started,
                        exc,
                    )
                raise
            finally:
                if execution_hooks is not None:
                    await _invoke_lifecycle_hook(
                        execution_hooks.on_execution_finished,
                        started,
                    )
            recovered += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            log_event(
                service._logger,
                logging.WARNING,
                failure_event_name,
                managed_thread_id=managed_thread_id,
                exc=exc,
            )
    rearmed_pending = 0
    if recover_pending_queue is not None:
        candidate_thread_ids = tuple(
            dict.fromkeys(
                filter(
                    None,
                    (
                        _normalized_optional_text(
                            getattr(binding, "thread_target_id", None)
                        )
                        for binding in _safe_list_bindings(
                            orchestration_service,
                            surface_kind=surface_kind,
                            include_disabled=False,
                            limit=1000,
                        )
                    ),
                )
            )
        )
        for managed_thread_id in candidate_thread_ids:
            if managed_thread_id in running_thread_ids:
                continue
            try:
                owned_surface_keys = find_surface_keys_for_pending_queue(
                    orchestration_service,
                    thread_store,
                    managed_thread_id=managed_thread_id,
                    surface_kind=surface_kind,
                    limit=1000,
                )
                if len(owned_surface_keys) != 1:
                    reason = (
                        "no_owned_surface_binding"
                        if not owned_surface_keys
                        else "ambiguous_surface_ownership"
                    )
                    log_event(
                        service._logger,
                        logging.INFO,
                        "chat.managed_thread.startup_pending_queue_skipped",
                        managed_thread_id=managed_thread_id,
                        surface_kind=surface_kind,
                        reason=reason,
                        owned_surface_keys=owned_surface_keys,
                        owned_surface_key_count=len(owned_surface_keys),
                    )
                    continue
                thread = orchestration_service.get_thread_target(managed_thread_id)
                if thread is None:
                    continue
                rearmed = recover_pending_queue(
                    service,
                    orchestration_service,
                    owned_surface_keys[0],
                    managed_thread_id,
                    thread,
                )
                if asyncio.iscoroutine(rearmed):
                    rearmed = await rearmed
                if rearmed:
                    rearmed_pending += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                log_event(
                    service._logger,
                    logging.WARNING,
                    failure_event_name,
                    managed_thread_id=managed_thread_id,
                    exc=exc,
                )
    if recovered or rearmed_pending or failed:
        log_event(
            service._logger,
            logging.INFO,
            finished_event_name,
            recovered=recovered,
            rearmed_pending=rearmed_pending,
            failed=failed,
        )


def _build_recovered_started_execution(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    thread: Any,
    recovered_execution: Any,
) -> Any:
    return SimpleNamespace(
        execution=SimpleNamespace(
            execution_id=managed_turn_id,
            backend_id=_normalized_optional_text(
                getattr(recovered_execution, "backend_id", None)
            ),
            status="running",
        ),
        thread=SimpleNamespace(
            thread_target_id=managed_thread_id,
            backend_thread_id=_normalized_optional_text(
                getattr(thread, "backend_thread_id", None)
            ),
            agent_id=getattr(thread, "agent_id", None),
        ),
        request=SimpleNamespace(
            message_text="",
            model=None,
        ),
        workspace_root=getattr(thread, "workspace_root", None),
    )
