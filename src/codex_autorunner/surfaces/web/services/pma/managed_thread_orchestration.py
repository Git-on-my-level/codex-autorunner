from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

from fastapi import Request

from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.orchestration import build_harness_backed_orchestration_service
from .....core.orchestration.catalog import RuntimeAgentDescriptor
from .common import normalize_optional_text
from .container import get_pma_request_context

logger = logging.getLogger(__name__)


async def cleanup_failed_provisioned_worktree(
    request: Request,
    *,
    worktree_repo_id: Optional[str],
) -> None:
    normalized_repo_id = normalize_optional_text(worktree_repo_id)
    if normalized_repo_id is None:
        return
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        return
    try:
        await asyncio.to_thread(
            supervisor.retire_worktree,
            worktree_repo_id=normalized_repo_id,
            delete_branch=True,
        )
    except Exception as exc:
        logger.warning(
            "Failed to clean up provisioned PMA worktree %s after thread creation failed: %s",
            normalized_repo_id,
            exc,
        )


def build_managed_thread_orchestration_service(request: Request) -> Any:
    context = get_pma_request_context(request)
    try:
        descriptors = get_registered_agents(context.agent_context)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()

    def _make_harness(agent_id: str, profile: Optional[str] = None) -> Any:
        cache = context.managed_thread_harness_cache
        resolution = resolve_agent_runtime(
            agent_id,
            profile,
            context=context.agent_context,
        )
        use_logical_profile_descriptor = (
            profile is not None and resolution.logical_agent_id == agent_id
        )
        descriptor_agent_id = (
            resolution.logical_agent_id
            if use_logical_profile_descriptor
            else resolution.runtime_agent_id
        )
        descriptor_profile = (
            resolution.logical_profile
            if use_logical_profile_descriptor
            else resolution.runtime_profile
        )
        cache_key = (descriptor_agent_id, descriptor_profile or "")
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        descriptor = descriptors.get(descriptor_agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{descriptor_agent_id}'")
        harness = descriptor.make_harness(
            wrap_requested_agent_context(
                context.agent_context,
                agent_id=resolution.logical_agent_id,
                profile=resolution.logical_profile,
            )
        )
        cache[cache_key] = harness
        return harness

    return build_harness_backed_orchestration_service(
        descriptors=cast(dict[str, RuntimeAgentDescriptor], descriptors),
        harness_factory=_make_harness,
        managed_thread_store=context.thread_store(),
    )


__all__ = [
    "build_managed_thread_orchestration_service",
    "cleanup_failed_provisioned_worktree",
]
