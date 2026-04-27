from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def create_thread_target_with_profile_fallback(
    orchestration_service: Any,
    agent_id: str,
    workspace_root: Path,
    *,
    repo_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    display_name: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
    context_profile: Optional[Any] = None,
    metadata: Optional[dict[str, Any]] = None,
    key_error: Optional[KeyError] = None,
) -> Any:
    agent_profile = (
        str((metadata or {}).get("agent_profile") or "").strip().lower()
        if isinstance(metadata, dict)
        else ""
    )
    if not agent_profile:
        if key_error is not None:
            raise key_error
        raise KeyError("agent_profile")

    catalog = getattr(orchestration_service, "definition_catalog", None)
    if catalog is None:
        raise RuntimeError(
            "orchestration_service missing definition_catalog for profile fallback"
        )
    get_definition = getattr(catalog, "get_definition", None)
    if not callable(get_definition):
        raise RuntimeError(
            "definition_catalog missing get_definition for profile fallback"
        )
    definition = get_definition(agent_id)
    if definition is None:
        raise (
            key_error
            if key_error is not None
            else KeyError(f"Unknown agent definition '{agent_id}'")
        )
    caps = getattr(definition, "capabilities", frozenset())
    if "durable_threads" not in caps:
        raise ValueError(
            f"Agent definition '{agent_id}' does not support durable_threads"
        )

    thread_store = getattr(orchestration_service, "thread_store", None)
    if thread_store is None:
        raise RuntimeError(
            "orchestration_service missing thread_store for profile fallback"
        )
    create_direct = getattr(thread_store, "create_thread_target", None)
    if not callable(create_direct):
        raise RuntimeError(
            "thread_store missing create_thread_target for profile fallback"
        )
    return create_direct(
        agent_id,
        workspace_root,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        display_name=display_name,
        backend_thread_id=backend_thread_id,
        context_profile=context_profile,
        metadata=metadata,
    )
