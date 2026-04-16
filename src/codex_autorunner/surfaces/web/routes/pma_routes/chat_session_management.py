from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request

from .....agents.registry import resolve_agent_runtime
from .....core.pma_lifecycle import PmaLifecycleRouter
from .....core.text_utils import _normalize_optional_text
from .....integrations.app_server.threads import pma_base_key
from .hermes_supervisors import resolve_cached_hermes_supervisor

logger = logging.getLogger(__name__)


def _resolve_hermes_supervisor(
    request: Request,
    *,
    profile: Optional[str],
):
    return resolve_cached_hermes_supervisor(request, profile=profile)


async def maybe_fork_hermes_pma_session(
    request: Request,
    *,
    current: dict[str, Any],
    agent: Optional[str],
    profile: Optional[str],
    hub_root: Path,
    stored_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    current_agent = _normalize_optional_text(current.get("agent"))
    current_profile = _normalize_optional_text(current.get("profile"))
    current_thread_id = _normalize_optional_text(current.get("thread_id"))
    requested_runtime = resolve_agent_runtime(
        agent or current_agent or "codex",
        profile,
        context=request.app.state,
    )
    if requested_runtime.logical_agent_id != "hermes":
        return {}

    current_runtime = None
    if current_agent:
        current_runtime = resolve_agent_runtime(
            current_agent,
            current_profile,
            context=request.app.state,
        )
    if current_runtime is not None and (
        current_runtime.logical_agent_id != requested_runtime.logical_agent_id
        or current_runtime.logical_profile != requested_runtime.logical_profile
    ):
        return {}

    if not current_thread_id:
        current_thread_id = _normalize_optional_text(stored_thread_id)
    if not current_thread_id:
        current_thread_id = _normalize_optional_text(
            request.app.state.app_server_threads.get_thread_id(
                pma_base_key(
                    requested_runtime.logical_agent_id,
                    requested_runtime.logical_profile,
                )
            )
        )
    if not current_thread_id:
        return {}

    if (
        current_runtime is not None
        and current_runtime.logical_profile != requested_runtime.logical_profile
    ):
        return {}

    supervisor = _resolve_hermes_supervisor(
        request,
        profile=requested_runtime.logical_profile,
    )
    if supervisor is None:
        return {}

    forked = await supervisor.fork_session(
        hub_root,
        current_thread_id,
        title="PMA session",
        metadata={
            "flow_type": "pma_new_session",
            "source_session_id": current_thread_id,
            "agent_profile": requested_runtime.logical_profile,
        },
    )
    if forked is None or not forked.session_id:
        return {}

    request.app.state.app_server_threads.set_thread_id(
        pma_base_key(
            requested_runtime.logical_agent_id,
            requested_runtime.logical_profile,
        ),
        forked.session_id,
    )
    return {
        "forked": True,
        "source_thread_id": current_thread_id,
        "thread_id": forked.session_id,
    }


def resolve_preclear_hermes_fork_thread_id(
    request: Request,
    *,
    current: dict[str, Any],
    agent: Optional[str],
    profile: Optional[str],
) -> Optional[str]:
    requested_runtime = resolve_agent_runtime(
        agent or _normalize_optional_text(current.get("agent")) or "codex",
        profile or _normalize_optional_text(current.get("profile")),
        context=request.app.state,
    )
    if requested_runtime.logical_agent_id != "hermes":
        return None
    return _normalize_optional_text(
        request.app.state.app_server_threads.get_thread_id(
            pma_base_key(
                requested_runtime.logical_agent_id,
                requested_runtime.logical_profile,
            )
        )
    )


async def build_new_session_details(
    request: Request,
    *,
    current: dict[str, Any],
    agent: Optional[str],
    profile: Optional[str],
    hub_root: Path,
    stored_thread_id: Optional[str] = None,
) -> dict[str, Any]:
    return await maybe_fork_hermes_pma_session(
        request,
        current=current,
        agent=agent,
        profile=profile,
        hub_root=hub_root,
        stored_thread_id=stored_thread_id,
    )


def serialize_lifecycle_result(
    result: Any,
    *,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "status": result.status,
        "message": result.message,
        "artifact_path": str(result.artifact_path) if result.artifact_path else None,
        "details": result.details if details is None else details,
    }


async def new_pma_session_response(
    request: Request,
    payload: Any,
    *,
    current: dict[str, Any],
    preclear_thread_id: Optional[str],
) -> dict[str, Any]:

    agent = _normalize_optional_text(payload.agent if payload else None)
    profile = _normalize_optional_text(payload.profile if payload else None)
    lane_id = ((payload.lane_id if payload else None) or "pma:default").strip()

    hub_root = request.app.state.config.root
    lifecycle_router = PmaLifecycleRouter(hub_root)
    result = await lifecycle_router.new(
        agent=agent,
        profile=profile,
        lane_id=lane_id,
    )
    if result.status != "ok":
        raise HTTPException(status_code=500, detail=result.error)

    details = dict(result.details)
    details.update(
        await build_new_session_details(
            request,
            current=current,
            agent=agent,
            profile=profile,
            hub_root=hub_root,
            stored_thread_id=preclear_thread_id,
        )
    )
    return serialize_lifecycle_result(result, details=details)
