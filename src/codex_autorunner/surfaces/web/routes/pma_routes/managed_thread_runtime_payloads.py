from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, cast

from fastapi import HTTPException, Request

from .....agents.managed_runtime import sync_managed_workspace_compat_files
from .....core.car_context import (
    build_car_context_bundle,
    default_managed_thread_context_profile,
    normalize_car_context_profile,
    render_injected_car_context,
)
from .....core.orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
)
from .....core.pma_context import format_pma_discoverability_preamble
from .....core.pma_thread_store import ManagedThreadNotActiveError
from .....integrations.chat.approval_modes import resolve_approval_mode_policies
from ...schemas import PmaManagedThreadMessageRequest
from ...services.pma.common import pma_config_from_raw as shared_pma_config_from_raw
from ...services.pma.managed_thread_followup import (
    resolve_managed_thread_followup_policy,
)
from .automation_adapter import normalize_optional_text

MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"


@dataclass(frozen=True)
class ManagedThreadMessageOptions:
    busy_policy: Literal["queue", "interrupt", "reject"]
    message: str
    notify_on: Optional[str]
    notify_lane: Optional[str]
    notify_once: bool
    notify_required: bool
    defer_execution: bool
    model: Optional[str]
    reasoning: Optional[str]
    agent_profile: Optional[str]
    context_profile: Any
    context_bundle: Any
    approval_policy: Optional[str]
    sandbox_policy: Optional[Any]
    live_backend_thread_id: str
    execution_prompt: str
    delivery_payload: dict[str, Any]


def get_pma_route_config(request: Request) -> dict[str, Any]:
    raw = getattr(request.app.state.config, "raw", {})
    return shared_pma_config_from_raw(raw)


def _resolve_managed_thread_policies(
    thread: dict[str, Any],
) -> tuple[Optional[str], Optional[Any]]:
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return resolve_approval_mode_policies(
        normalize_optional_text(
            thread.get("approval_mode") or metadata.get("approval_mode")
        ),
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
        override_approval_policy=normalize_optional_text(
            thread.get("approval_policy") or metadata.get("approval_policy")
        ),
        override_sandbox_policy=(
            thread.get("sandbox_policy")
            if thread.get("sandbox_policy") is not None
            else metadata.get("sandbox_policy")
        ),
    )


def _compose_compacted_prompt(compact_seed: str, message: str) -> str:
    return (
        "Context summary (from compaction):\n"
        f"{compact_seed}\n\n"
        "User message:\n"
        f"{message}"
    )


def _compose_execution_prompt(
    *,
    agent: Any,
    hub_root: Path,
    stored_backend_id: Optional[str],
    compact_seed: Optional[str],
    message: str,
    context_bundle: Any,
) -> str:
    execution_message = message
    if not stored_backend_id and compact_seed:
        execution_message = _compose_compacted_prompt(compact_seed, message)

    if str(agent or "").strip().lower() == "zeroclaw":
        return execution_message

    preamble = format_pma_discoverability_preamble(hub_root=hub_root)
    user_message = f"<user_message>\n{execution_message}\n</user_message>\n"
    car_context = render_injected_car_context(context_bundle)
    if not car_context:
        return f"{preamble}{user_message}"
    return f"{preamble}{car_context}\n\n{user_message}"


def get_live_thread_runtime_binding(service: Any, managed_thread_id: str) -> Any:
    getter = getattr(service, "get_thread_runtime_binding", None)
    if not callable(getter):
        return None
    return getter(managed_thread_id)


def sanitize_managed_thread_result_error(detail: Any) -> str:
    sanitized = normalize_optional_text(detail)
    if sanitized in {RUNTIME_THREAD_TIMEOUT_ERROR, "PMA chat timed out"}:
        return "PMA chat timed out"
    if sanitized in {RUNTIME_THREAD_INTERRUPTED_ERROR, "PMA chat interrupted"}:
        return "PMA chat interrupted"
    if sanitized in {"PMA chat timed out", "PMA chat interrupted"}:
        return sanitized
    return MANAGED_THREAD_PUBLIC_EXECUTION_ERROR


def normalize_busy_policy(value: Any) -> Literal["queue", "interrupt", "reject"]:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return "queue"
    busy_policy = normalized.lower()
    if busy_policy not in {"queue", "interrupt", "reject"}:
        raise HTTPException(
            status_code=400,
            detail="busy_policy must be one of: queue, interrupt, reject",
        )
    return cast(Literal["queue", "interrupt", "reject"], busy_policy)


def build_interrupt_failure_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: Optional[str],
    backend_thread_id: str,
    detail: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "rejected",
        "interrupt_state": "failed",
        "execution_state": "running",
        "reason": "interrupt_failed",
        "detail": detail,
        "next_step": (
            "Wait for the active turn to finish, inspect thread status, "
            "or retry the interrupt after checking runtime health."
        ),
        "active_turn_status": "running",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "active_managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
        **delivery_payload,
    }


def resolve_managed_thread_message_options(
    request: Request,
    payload: PmaManagedThreadMessageRequest,
    *,
    managed_thread_id: str,
    thread: dict[str, Any],
    service: Any,
) -> ManagedThreadMessageOptions:
    busy_policy = normalize_busy_policy(payload.busy_policy)
    message = payload.message or ""
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    defaults = get_pma_route_config(request)
    max_text_chars = int(defaults.get("max_text_chars", 0) or 0)
    if max_text_chars > 0 and len(message) > max_text_chars:
        raise HTTPException(
            status_code=400,
            detail=f"message exceeds max_text_chars ({max_text_chars} characters)",
        )

    followup_policy = resolve_managed_thread_followup_policy(
        payload,
        default_terminal_followup=bool(
            defaults.get("managed_thread_terminal_followup_default")
        ),
    )
    notify_on = followup_policy.event_mode
    notify_lane = followup_policy.lane_id
    notify_once = followup_policy.notify_once
    defer_execution = bool(payload.defer_execution)
    model = normalize_optional_text(payload.model) or defaults.get("model")
    reasoning = normalize_optional_text(payload.reasoning) or defaults.get("reasoning")
    compact_seed = normalize_optional_text(thread.get("compact_seed"))
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    agent_profile = normalize_optional_text(
        thread.get("agent_profile") or metadata.get("agent_profile")
    )
    context_profile = normalize_car_context_profile(
        thread.get("context_profile") or metadata.get("context_profile"),
        default=default_managed_thread_context_profile(
            resource_kind=thread.get("resource_kind")
        ),
    )
    context_bundle = build_car_context_bundle(
        context_profile,
        prompt_text=message,
    )
    approval_policy, sandbox_policy = _resolve_managed_thread_policies(thread)
    runtime_binding = get_live_thread_runtime_binding(service, managed_thread_id)
    live_backend_thread_id = (
        normalize_optional_text(getattr(runtime_binding, "backend_thread_id", None))
        or ""
    )
    execution_prompt = _compose_execution_prompt(
        agent=thread.get("agent"),
        hub_root=request.app.state.config.root,
        stored_backend_id=live_backend_thread_id,
        compact_seed=compact_seed,
        message=message,
        context_bundle=context_bundle,
    )

    return ManagedThreadMessageOptions(
        busy_policy=busy_policy,
        message=message,
        notify_on=notify_on,
        notify_lane=notify_lane,
        notify_once=notify_once,
        notify_required=followup_policy.required,
        defer_execution=defer_execution,
        model=model,
        reasoning=reasoning,
        agent_profile=agent_profile,
        context_profile=context_profile,
        context_bundle=context_bundle,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        live_backend_thread_id=live_backend_thread_id,
        execution_prompt=execution_prompt,
        delivery_payload={"delivered_message": message},
    )


def sync_zeroclaw_context_if_needed(
    *,
    thread: dict[str, Any],
    options: ManagedThreadMessageOptions,
) -> None:
    if str(thread.get("agent") or "").strip().lower() != "zeroclaw":
        return
    workspace_root = normalize_optional_text(thread.get("workspace_root"))
    if workspace_root is None:
        return
    sync_managed_workspace_compat_files(
        "zeroclaw",
        runtime_workspace_root=Path(workspace_root) / "workspace",
        bundle=options.context_bundle,
    )


def build_archived_thread_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
) -> dict[str, Any]:
    detail = "Managed thread is archived and read-only"
    return {
        "status": "error",
        "send_state": "rejected",
        "reason": "thread_archived",
        "detail": detail,
        "next_step": "Use `car pma thread resume` or spawn a new thread.",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
    }


def build_not_active_thread_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    exc: ManagedThreadNotActiveError,
) -> dict[str, Any]:
    detail = (
        "Managed thread is archived and read-only"
        if exc.status == "archived"
        else "Managed thread is not active"
    )
    return {
        "status": "error",
        "send_state": "rejected",
        "reason": "thread_not_active",
        "detail": detail,
        "next_step": "Resume the thread or create a new active thread.",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
    }


def build_running_turn_exists_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    running_turn: Optional[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "already_in_flight",
        "reason": "running_turn_exists",
        "detail": f"Managed thread {managed_thread_id} already has a running turn",
        "next_step": (
            "Wait for the running turn to finish or let the default terminal "
            "wakeup fire; use --watch only for foreground babysitting."
        ),
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": str((running_turn or {}).get("managed_turn_id") or "")
        or None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": "Managed thread already has a running turn",
    }


def build_execution_setup_error_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "accepted",
        "execution_state": "completed",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
        **delivery_payload,
    }


def build_started_execution_error_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    error: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "accepted",
        "execution_state": "completed",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": error,
        **delivery_payload,
    }


def build_queued_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    queue_depth: int,
    active_managed_turn_id: Optional[str],
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "queued",
        "execution_state": "queued",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
        "queue_depth": queue_depth,
        "active_managed_turn_id": active_managed_turn_id,
    }
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_accepted_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "accepted",
        "execution_state": "running",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
    }
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_enqueued_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    execution_state: str,
    queue_depth: Optional[int] = None,
    active_managed_turn_id: Optional[str] = None,
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "enqueued",
        "execution_state": execution_state,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
    }
    if queue_depth is not None:
        payload["queue_depth"] = queue_depth
    if active_managed_turn_id is not None:
        payload["active_managed_turn_id"] = active_managed_turn_id
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_execution_result_payload(
    *,
    status: str,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    assistant_text: str,
    error: Optional[str],
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": assistant_text,
        "error": error,
        **response_payload,
    }


__all__ = [
    "MANAGED_THREAD_PUBLIC_EXECUTION_ERROR",
    "ManagedThreadMessageOptions",
    "build_accepted_send_payload",
    "build_archived_thread_payload",
    "build_enqueued_send_payload",
    "build_execution_result_payload",
    "build_execution_setup_error_payload",
    "build_interrupt_failure_payload",
    "build_not_active_thread_payload",
    "build_queued_send_payload",
    "build_running_turn_exists_payload",
    "build_started_execution_error_payload",
    "get_live_thread_runtime_binding",
    "get_pma_route_config",
    "normalize_busy_policy",
    "resolve_managed_thread_message_options",
    "sanitize_managed_thread_result_error",
    "sync_zeroclaw_context_if_needed",
]
