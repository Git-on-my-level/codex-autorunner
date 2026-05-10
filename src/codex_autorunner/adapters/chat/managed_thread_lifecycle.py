from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True)
class ManagedThreadBindingResolution:
    binding: Optional[Any]
    thread: Optional[Any]


@dataclass(frozen=True)
class ManagedThreadReplacementResult:
    had_previous: bool
    previous_thread_id: Optional[str]
    replacement_thread: Any
    stop_outcome: Optional[Any] = None


def resolve_surface_thread_binding(
    orchestration_service: Any,
    *,
    surface_kind: str,
    surface_key: str,
    mode: Optional[str] = None,
    binding: Optional[Any] = None,
    thread: Optional[Any] = None,
) -> ManagedThreadBindingResolution:
    resolved_binding = binding
    if resolved_binding is None:
        get_binding = getattr(orchestration_service, "get_binding", None)
        if callable(get_binding):
            resolved_binding = get_binding(
                surface_kind=surface_kind,
                surface_key=surface_key,
            )
    normalized_mode = (
        mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
    )
    if resolved_binding is None:
        return ManagedThreadBindingResolution(binding=None, thread=None)
    if normalized_mode is not None:
        binding_mode = str(getattr(resolved_binding, "mode", "") or "").strip().lower()
        if binding_mode != normalized_mode:
            return ManagedThreadBindingResolution(binding=resolved_binding, thread=None)
    if thread is None:
        get_thread_target = getattr(orchestration_service, "get_thread_target", None)
        thread_target_id = str(
            getattr(resolved_binding, "thread_target_id", "") or ""
        ).strip()
        if callable(get_thread_target) and thread_target_id:
            thread = get_thread_target(thread_target_id)
    return ManagedThreadBindingResolution(binding=resolved_binding, thread=thread)


def bind_surface_thread(
    orchestration_service: Any,
    *,
    surface_kind: str,
    surface_key: str,
    thread_target_id: str,
    agent_id: str,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: Optional[str],
    metadata: Optional[dict[str, Any]] = None,
    backend_thread_id: Optional[str] = None,
    backend_runtime_instance_id: Optional[str] = None,
    thread: Optional[Any] = None,
) -> Any:
    provided_thread = thread is not None
    if thread is None:
        thread = orchestration_service.get_thread_target(thread_target_id)
    if thread is None:
        raise KeyError(f"Unknown thread target '{thread_target_id}'")
    normalized_backend_thread_id = _normalized_optional_text(backend_thread_id)
    normalized_runtime_instance_id = _normalized_optional_text(
        backend_runtime_instance_id
    )
    current_backend_thread_id = _normalized_optional_text(
        getattr(thread, "backend_thread_id", None)
    )
    current_runtime_instance_id = _normalized_optional_text(
        getattr(thread, "backend_runtime_instance_id", None)
    )
    lifecycle_status = (
        str(getattr(thread, "lifecycle_status", "") or "").strip().lower()
    )
    should_resume = False if provided_thread else lifecycle_status != "active"
    should_resume = should_resume or (
        normalized_backend_thread_id != current_backend_thread_id
    )
    should_resume = should_resume or (
        normalized_runtime_instance_id != current_runtime_instance_id
    )
    if should_resume:
        thread = orchestration_service.resume_thread_target(
            thread_target_id,
            backend_thread_id=normalized_backend_thread_id,
            backend_runtime_instance_id=normalized_runtime_instance_id,
        )
    orchestration_service.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=thread.thread_target_id,
        agent_id=agent_id,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mode=mode,
        metadata=dict(metadata or {}),
    )
    return thread


async def replace_surface_thread(
    orchestration_service: Any,
    *,
    surface_kind: str,
    surface_key: str,
    workspace_root: Path,
    agent_id: str,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: Optional[str],
    display_name: str,
    binding_metadata: Optional[dict[str, Any]] = None,
    thread_metadata: Optional[dict[str, Any]] = None,
    binding: Optional[Any] = None,
    thread: Optional[Any] = None,
) -> ManagedThreadReplacementResult:
    resolved = resolve_surface_thread_binding(
        orchestration_service,
        surface_kind=surface_kind,
        surface_key=surface_key,
        mode=mode,
        binding=binding,
        thread=thread,
    )
    current_thread = resolved.thread
    stop_outcome = None
    previous_thread_id = None
    if current_thread is not None:
        previous_thread_id = str(getattr(current_thread, "thread_target_id", "") or "")
        if previous_thread_id:
            stop_outcome = await orchestration_service.stop_thread(previous_thread_id)
            orchestration_service.archive_thread_target(previous_thread_id)
    replacement = orchestration_service.create_thread_target(
        agent_id,
        workspace_root,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        display_name=display_name,
        metadata=dict(thread_metadata or {}) or None,
    )
    bound_thread = bind_surface_thread(
        orchestration_service,
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=replacement.thread_target_id,
        agent_id=agent_id,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mode=mode,
        metadata=binding_metadata,
        thread=replacement,
    )
    return ManagedThreadReplacementResult(
        had_previous=current_thread is not None,
        previous_thread_id=previous_thread_id or None,
        replacement_thread=bound_thread,
        stop_outcome=stop_outcome,
    )


__all__ = [
    "ManagedThreadBindingResolution",
    "ManagedThreadReplacementResult",
    "bind_surface_thread",
    "replace_surface_thread",
    "resolve_surface_thread_binding",
]
