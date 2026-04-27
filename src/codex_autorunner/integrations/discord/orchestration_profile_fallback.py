from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def create_thread_target_with_profile_fallback(
    service: Any,
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
) -> Any:
    agent_profile = (
        str((metadata or {}).get("agent_profile") or "").strip().lower()
        if isinstance(metadata, dict)
        else ""
    )
    if not agent_profile:
        raise
    return service.thread_store.create_thread_target(
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
