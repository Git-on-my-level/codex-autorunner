"""Chat-core runtime helper utilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ...core.orchestration import WorkspaceRuntimeAcquisition


@dataclass(frozen=True)
class ChatThreadRuntimeBinding:
    backend_thread_id: Optional[str]
    backend_runtime_instance_id: Optional[str]
    runtime_available: bool
    used_requested_backend_thread_id: bool


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _thread_matches_runtime_owner(
    thread: Any,
    *,
    workspace_root: Path,
    agent_id: str,
    agent_profile: Optional[str],
) -> bool:
    if thread is None:
        return False
    if str(getattr(thread, "agent_id", "") or "").strip() != agent_id:
        return False
    if (getattr(thread, "agent_profile", None) or None) != (agent_profile or None):
        return False
    return str(getattr(thread, "workspace_root", "") or "").strip() == str(
        workspace_root.resolve()
    )


async def acquire_chat_workspace_runtime(
    orchestration_service: Any,
    *,
    agent_id: str,
    workspace_root: Path,
) -> WorkspaceRuntimeAcquisition:
    acquire = getattr(orchestration_service, "acquire_workspace_runtime", None)
    if callable(acquire):
        acquired = await acquire(agent_id, workspace_root)
        if isinstance(acquired, WorkspaceRuntimeAcquisition):
            return acquired
        return WorkspaceRuntimeAcquisition(
            harness=acquired.harness,
            backend_runtime_instance_id=_normalized_optional_text(
                getattr(acquired, "backend_runtime_instance_id", None)
            ),
        )
    resolver = getattr(
        orchestration_service,
        "resolve_backend_runtime_instance_id",
        None,
    )
    runtime_instance_id = None
    if callable(resolver):
        runtime_instance_id = await resolver(agent_id, workspace_root)
    harness_factory = getattr(orchestration_service, "_harness_for_agent", None)
    harness = (
        harness_factory(agent_id) if callable(harness_factory) else cast(Any, object())
    )
    return WorkspaceRuntimeAcquisition(
        harness=harness,
        backend_runtime_instance_id=_normalized_optional_text(runtime_instance_id),
    )


async def resolve_chat_thread_runtime_binding(
    orchestration_service: Any,
    *,
    agent_id: str,
    workspace_root: Path,
    requested_backend_thread_id: Optional[str],
    existing_thread: Any = None,
    agent_profile: Optional[str] = None,
) -> ChatThreadRuntimeBinding:
    normalized_requested_backend_thread_id = _normalized_optional_text(
        requested_backend_thread_id
    )
    if normalized_requested_backend_thread_id is None:
        return ChatThreadRuntimeBinding(
            backend_thread_id=None,
            backend_runtime_instance_id=None,
            runtime_available=False,
            used_requested_backend_thread_id=False,
        )
    runtime = await acquire_chat_workspace_runtime(
        orchestration_service,
        agent_id=agent_id,
        workspace_root=workspace_root,
    )
    runtime_instance_id = _normalized_optional_text(runtime.backend_runtime_instance_id)
    if runtime_instance_id is not None:
        return ChatThreadRuntimeBinding(
            backend_thread_id=normalized_requested_backend_thread_id,
            backend_runtime_instance_id=runtime_instance_id,
            runtime_available=True,
            used_requested_backend_thread_id=True,
        )
    existing_backend_thread_id = _normalized_optional_text(
        getattr(existing_thread, "backend_thread_id", None)
    )
    if (
        existing_backend_thread_id is not None
        and existing_backend_thread_id != normalized_requested_backend_thread_id
        and _thread_matches_runtime_owner(
            existing_thread,
            workspace_root=workspace_root,
            agent_id=agent_id,
            agent_profile=agent_profile,
        )
    ):
        return ChatThreadRuntimeBinding(
            backend_thread_id=existing_backend_thread_id,
            backend_runtime_instance_id=None,
            runtime_available=False,
            used_requested_backend_thread_id=False,
        )
    return ChatThreadRuntimeBinding(
        backend_thread_id=normalized_requested_backend_thread_id,
        backend_runtime_instance_id=None,
        runtime_available=False,
        used_requested_backend_thread_id=True,
    )


def iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """Collect exception causes/contexts from outermost to innermost."""

    chain: list[BaseException] = []
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


__all__ = [
    "ChatThreadRuntimeBinding",
    "acquire_chat_workspace_runtime",
    "iter_exception_chain",
    "resolve_chat_thread_runtime_binding",
]
