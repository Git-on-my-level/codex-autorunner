from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....core.time_utils import now_iso
from ...services.pma.common import (
    normalize_optional_text as service_normalize_optional_text,
)

logger = logging.getLogger(__name__)


async def await_if_needed(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


async def call_with_fallbacks(
    method: Any, attempts: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> Any:
    last_type_error: Optional[TypeError] = None
    for args, kwargs in attempts:
        try:
            return await await_if_needed(method(*args, **kwargs))
        except TypeError as exc:
            last_type_error = exc
            continue
    if last_type_error is not None:
        raise last_type_error
    raise RuntimeError("No automation method call attempts were provided")


def first_callable(target: Any, names: tuple[str, ...]) -> Optional[Any]:
    for name in names:
        candidate = getattr(target, name, None)
        if callable(candidate):
            return candidate
    return None


def discover_automation_store_class() -> Optional[type[Any]]:
    candidates: tuple[tuple[str, str], ...] = (
        ("codex_autorunner.core.pma_automation_store", "PmaAutomationStore"),
        ("codex_autorunner.core.pma_automation", "PmaAutomationStore"),
        ("codex_autorunner.core.automation_store", "AutomationStore"),
        ("codex_autorunner.core.automation", "AutomationStore"),
        ("codex_autorunner.core.hub_automation", "HubAutomationStore"),
    )
    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        klass = getattr(module, class_name, None)
        if isinstance(klass, type):
            return klass
    return None


async def call_store_create_with_payload(
    store: Any, method_names: tuple[str, ...], payload: dict[str, Any]
) -> Any:
    method = first_callable(store, method_names)
    if method is None:
        raise HTTPException(status_code=503, detail="Automation action unavailable")
    return await call_with_fallbacks(
        method,
        [
            ((payload,), {}),
            ((), {"payload": payload}),
            ((), dict(payload)),
        ],
    )


async def call_store_list(
    store: Any, method_names: tuple[str, ...], filters: dict[str, Any]
) -> Any:
    method = first_callable(store, method_names)
    if method is None:
        raise HTTPException(status_code=503, detail="Automation action unavailable")
    return await call_with_fallbacks(
        method,
        [
            ((), dict(filters)),
            ((dict(filters),), {}),
            ((), {}),
        ],
    )


async def call_store_action_with_id(
    store: Any,
    method_names: tuple[str, ...],
    item_id: str,
    payload: dict[str, Any],
    *,
    id_aliases: tuple[str, ...],
) -> Any:
    method = first_callable(store, method_names)
    if method is None:
        raise HTTPException(status_code=503, detail="Automation action unavailable")
    item_kwargs: dict[str, Any] = {}
    for alias in id_aliases:
        item_kwargs[alias] = item_id
    merged_with_id = dict(payload)
    if id_aliases:
        merged_with_id[id_aliases[0]] = item_id
    return await call_with_fallbacks(
        method,
        [
            ((item_id, dict(payload)), {}),
            ((item_id,), dict(payload)),
            ((item_id,), {"payload": dict(payload)}),
            ((), item_kwargs),
            ((), merged_with_id),
            ((item_id,), {}),
        ],
    )


async def get_automation_store(
    request: Request,
    runtime_state: Any,
    *,
    required: bool = True,
) -> Optional[Any]:
    pma_automation_store = (
        getattr(runtime_state, "pma_automation_store", None)
        if runtime_state is not None
        else None
    )
    pma_automation_root = (
        getattr(runtime_state, "pma_automation_root", None)
        if runtime_state is not None
        else None
    )

    hub_root = request.app.state.config.root
    supervisor = getattr(request.app.state, "hub_supervisor", None)
    if supervisor is not None:
        for name in ("get_pma_automation_store", "get_automation_store"):
            accessor = getattr(supervisor, name, None)
            if not callable(accessor):
                continue
            for args in ((), (hub_root,)):
                try:
                    store = await await_if_needed(accessor(*args))
                except TypeError:
                    continue
                except Exception:
                    logger.exception("Failed to resolve automation store from %s", name)
                    break
                if store is not None:
                    return store
                break
        for name in ("pma_automation_store", "automation_store"):
            store = getattr(supervisor, name, None)
            if store is not None:
                return store

    if pma_automation_store is not None and pma_automation_root == hub_root:
        return pma_automation_store

    klass = discover_automation_store_class()
    if klass is not None:
        for args in ((hub_root,), ()):
            try:
                store = klass(*args)
            except TypeError:
                continue
            except Exception:
                logger.exception("Failed to initialize automation store")
                break
            if runtime_state is not None:
                runtime_state.pma_automation_store = store
                runtime_state.pma_automation_root = hub_root
            return store

    if required:
        raise HTTPException(status_code=503, detail="Hub automation store unavailable")
    return None


def normalize_optional_text(value: Any) -> Optional[str]:
    return service_normalize_optional_text(value)


async def notify_hub_automation_transition(
    request: Request,
    runtime_state: Any,
    *,
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    from_state: str,
    to_state: str,
    reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    payload: dict[str, Any] = {
        "from_state": (from_state or "").strip(),
        "to_state": (to_state or "").strip(),
        "reason": normalize_optional_text(reason) or "",
        "timestamp": normalize_optional_text(timestamp) or now_iso(),
    }
    normalized_repo_id = normalize_optional_text(repo_id)
    normalized_run_id = normalize_optional_text(run_id)
    normalized_thread_id = normalize_optional_text(thread_id)
    if normalized_repo_id:
        payload["repo_id"] = normalized_repo_id
    if normalized_run_id:
        payload["run_id"] = normalized_run_id
    if normalized_thread_id:
        payload["thread_id"] = normalized_thread_id
    if isinstance(extra, dict):
        payload.update(extra)

    supervisor = getattr(request.app.state, "hub_supervisor", None)
    store = await get_automation_store(request, runtime_state, required=False)
    if store is None:
        return

    method = first_callable(
        store,
        ("notify_transition",),
    )
    if method is None:
        return
    try:
        await call_with_fallbacks(
            method,
            [
                ((dict(payload),), {}),
                ((), {"payload": dict(payload)}),
                ((), dict(payload)),
            ],
        )
    except Exception:
        logger.exception("Failed to notify hub automation transition")
        return

    process_now = (
        getattr(supervisor, "process_pma_automation_now", None)
        if supervisor is not None
        else None
    )
    if not callable(process_now):
        return
    try:
        await await_if_needed(process_now(include_timers=False))
    except TypeError:
        try:
            await await_if_needed(process_now())
        except Exception:
            logger.exception("Failed immediate PMA automation processing")
    except Exception:
        logger.exception("Failed immediate PMA automation processing")


async def notify_managed_thread_terminal_transition(
    request: Request,
    runtime_state: Any,
    *,
    thread: dict[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    to_state: str,
    reason: str,
) -> None:
    normalized_to_state = (to_state or "").strip().lower() or "failed"
    await notify_hub_automation_transition(
        request,
        runtime_state,
        repo_id=normalize_optional_text(thread.get("repo_id")),
        run_id=None,
        thread_id=managed_thread_id,
        from_state="running",
        to_state=normalized_to_state,
        reason=reason,
        extra={
            "event_type": f"managed_thread_{normalized_to_state}",
            "transition_id": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
            "idempotency_key": (
                f"managed_turn:{managed_turn_id}:{normalized_to_state}"
            ),
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "agent": normalize_optional_text(thread.get("agent")) or "",
        },
    )
