from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from .....agents.managed_runtime import sync_managed_workspace_compat_files
from .....core.pma.attachments import (
    normalize_managed_thread_attachments as _core_normalize_managed_thread_attachments,
)
from .....core.pma.message_options import (
    ManagedThreadMessageInput,
    ManagedThreadMessageOptions,
)
from .....core.pma.message_options import (
    resolve_managed_thread_message_options as _core_resolve_managed_thread_message_options,
)
from .....core.pma.outbound_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    build_accepted_send_payload,
    build_archived_thread_payload,
    build_enqueued_send_payload,
    build_execution_result_payload,
    build_execution_setup_error_payload,
    build_interrupt_failure_payload,
    build_not_active_thread_payload,
    build_queued_send_payload,
    build_running_turn_exists_payload,
    build_started_execution_error_payload,
    sanitize_managed_thread_result_error,
)
from .....core.pma.policies import normalize_busy_policy as _core_normalize_busy_policy
from .....core.text_utils import _normalize_optional_text
from .....integrations.chat.approval_modes import resolve_approval_mode_policies
from ...schemas import PmaManagedThreadMessageRequest
from ...services.pma.common import pma_config_from_raw as shared_pma_config_from_raw
from ...services.pma.managed_thread_followup import (
    resolve_managed_thread_followup_policy,
)


def normalize_managed_thread_attachments(value: Any) -> list[dict[str, Any]]:
    try:
        return _core_normalize_managed_thread_attachments(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def normalize_busy_policy(value: Any) -> Any:
    try:
        return _core_normalize_busy_policy(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_pma_route_config(request: Request) -> dict[str, Any]:
    raw = getattr(request.app.state.config, "raw", {})
    return shared_pma_config_from_raw(raw)


def get_live_thread_runtime_binding(service: Any, managed_thread_id: str) -> Any:
    getter = getattr(service, "get_thread_runtime_binding", None)
    if not callable(getter):
        return None
    return getter(managed_thread_id)


def _resolve_managed_thread_policies(
    thread: dict[str, Any],
) -> tuple[str | None, Any | None]:
    metadata = thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return resolve_approval_mode_policies(
        _normalize_optional_text(
            thread.get("approval_mode") or metadata.get("approval_mode")
        ),
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
        override_approval_policy=_normalize_optional_text(
            thread.get("approval_policy") or metadata.get("approval_policy")
        ),
        override_sandbox_policy=(
            thread.get("sandbox_policy")
            if thread.get("sandbox_policy") is not None
            else metadata.get("sandbox_policy")
        ),
    )


def resolve_managed_thread_message_options(
    request: Request,
    payload: PmaManagedThreadMessageRequest,
    *,
    managed_thread_id: str,
    thread: dict[str, Any],
    service: Any,
) -> ManagedThreadMessageOptions:
    defaults = get_pma_route_config(request)
    followup_policy = resolve_managed_thread_followup_policy(
        payload,
        default_terminal_followup=bool(
            defaults.get("managed_thread_terminal_followup_default")
        ),
    )
    runtime_binding = get_live_thread_runtime_binding(service, managed_thread_id)
    live_backend_thread_id = (
        _normalize_optional_text(getattr(runtime_binding, "backend_thread_id", None))
        or ""
    )
    approval_policy, sandbox_policy = _resolve_managed_thread_policies(thread)
    try:
        return _core_resolve_managed_thread_message_options(
            ManagedThreadMessageInput(
                message=payload.message,
                busy_policy=payload.busy_policy,
                notify_on=followup_policy.event_mode,
                notify_lane=followup_policy.lane_id,
                notify_once=followup_policy.notify_once,
                notify_required=followup_policy.required,
                defer_execution=bool(payload.defer_execution),
                model=payload.model,
                reasoning=payload.reasoning,
                attachments=payload.attachments,
                defaults=defaults,
                thread=thread,
                hub_root=request.app.state.config.root,
                runtime_cwd=(
                    Path(str(thread.get("workspace_root")))
                    if thread.get("workspace_root")
                    else None
                ),
                live_backend_thread_id=live_backend_thread_id,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def sync_zeroclaw_context_if_needed(
    *,
    thread: dict[str, Any],
    options: ManagedThreadMessageOptions,
) -> None:
    if str(thread.get("agent") or "").strip().lower() != "zeroclaw":
        return
    workspace_root = _normalize_optional_text(thread.get("workspace_root"))
    if workspace_root is None:
        return
    sync_managed_workspace_compat_files(
        "zeroclaw",
        runtime_workspace_root=Path(workspace_root) / "workspace",
        bundle=options.context_bundle,
    )


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
    "normalize_managed_thread_attachments",
    "resolve_managed_thread_message_options",
    "sanitize_managed_thread_result_error",
    "sync_zeroclaw_context_if_needed",
]
