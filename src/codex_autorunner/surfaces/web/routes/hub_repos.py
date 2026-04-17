from __future__ import annotations

import asyncio
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException

from ....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ..app_state import HubAppContext
from ..schemas import (
    HubArchiveRepoStateRequest,
    HubArchiveRepoStateResponse,
    HubArchiveWorktreeRequest,
    HubArchiveWorktreeResponse,
    HubArchiveWorktreeStateResponse,
    HubCleanupWorktreeRequest,
    HubCreateWorktreeRequest,
    HubDestinationSetRequest,
    HubJobResponse,
    RunControlRequest,
)
from .hub_repo_routes import (
    HubDestinationService,
    HubMountManager,
    HubRepoEnricher,
    HubRunControlService,
    HubWorktreeService,
    build_hub_agent_workspace_router,
    build_hub_channel_router,
    build_hub_repo_crud_router,
    build_hub_repo_listing_router,
)
from .hub_repo_routes.cache_coordinator import HubCacheCoordinator


def build_hub_repo_routes(
    context: HubAppContext,
    mount_manager: HubMountManager,
) -> APIRouter:
    router = APIRouter()

    def _route_method_path_pairs(target_router: APIRouter) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for route in target_router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or ()
            if not isinstance(path, str):
                continue
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                pairs.add((str(method), path))
        return pairs

    def _prune_overlapping_routes(
        target_router: APIRouter, overlapping_pairs: set[tuple[str, str]]
    ) -> None:
        filtered_routes = []
        for route in target_router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or ()
            if not isinstance(path, str):
                filtered_routes.append(route)
                continue
            route_pairs = {
                (str(method), path)
                for method in methods
                if method not in {"HEAD", "OPTIONS"}
            }
            if route_pairs and route_pairs.issubset(overlapping_pairs):
                continue
            filtered_routes.append(route)
        target_router.routes = filtered_routes

    enricher = HubRepoEnricher(context, mount_manager)
    cache = HubCacheCoordinator(context, enricher)
    run_control = HubRunControlService(
        context, mount_manager, enricher, cache_coordinator=cache
    )

    def _build_force_attestation_payload(
        attestation: Optional[str], *, target_scope: str
    ) -> Optional[dict[str, str]]:
        if attestation is None:
            return None
        return {
            "phrase": FORCE_ATTESTATION_REQUIRED_PHRASE,
            "user_request": attestation,
            "target_scope": target_scope,
        }

    worktree = HubWorktreeService(
        context, mount_manager, enricher, _build_force_attestation_payload
    )
    destination = HubDestinationService(context, mount_manager)

    @router.get("/hub/repos/{repo_id}/destination")
    async def get_repo_destination(repo_id: str):
        return await destination.get_repo_destination(repo_id)

    @router.post("/hub/repos/{repo_id}/destination")
    async def set_repo_destination(repo_id: str, payload: HubDestinationSetRequest):
        return await destination.set_repo_destination(repo_id, payload)

    @router.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        return await worktree.create_worktree(
            base_repo_id=payload.base_repo_id,
            branch=payload.branch,
            force=payload.force,
            start_point=payload.start_point,
        )

    @router.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        return await worktree.create_worktree_job(payload)

    @router.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        return await worktree.cleanup_worktree(
            worktree_repo_id=payload.worktree_repo_id,
            delete_branch=payload.delete_branch,
            delete_remote=payload.delete_remote,
            archive=payload.archive,
            force=payload.force,
            force_attestation=payload.force_attestation,
            force_archive=payload.force_archive,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post("/hub/jobs/worktrees/cleanup", response_model=HubJobResponse)
    async def cleanup_worktree_job(payload: HubCleanupWorktreeRequest):
        return await worktree.cleanup_worktree_job(payload)

    @router.post("/hub/worktrees/archive", response_model=HubArchiveWorktreeResponse)
    async def archive_worktree(payload: HubArchiveWorktreeRequest):
        return await worktree.archive_worktree(
            worktree_repo_id=payload.worktree_repo_id,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post(
        "/hub/worktrees/archive-state",
        response_model=HubArchiveWorktreeStateResponse,
    )
    async def archive_worktree_state(payload: HubArchiveWorktreeRequest):
        return await worktree.archive_worktree_state(
            worktree_repo_id=payload.worktree_repo_id,
            archive_note=payload.archive_note,
            archive_profile=payload.archive_profile,
        )

    @router.post(
        "/hub/repos/archive-state",
        response_model=HubArchiveRepoStateResponse,
    )
    async def archive_repo_state(payload: HubArchiveRepoStateRequest):
        try:
            result = await asyncio.to_thread(
                context.supervisor.archive_repo_state,
                repo_id=payload.repo_id,
                archive_note=payload.archive_note,
                archive_profile=payload.archive_profile,
            )
            await cache.invalidate_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/{repo_id}/cleanup-threads")
    async def cleanup_repo_threads(repo_id: str):
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_repo_threads,
                repo_id=repo_id,
            )
            await cache.invalidate_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/cleanup-threads")
    async def cleanup_all_repo_threads():
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_all_repo_threads
            )
            await cache.invalidate_caches()
            return result
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/hub/cleanup-all/preview")
    async def cleanup_all_preview():
        try:
            result = await asyncio.to_thread(
                context.supervisor.cleanup_all,
                dry_run=True,
            )
            return result
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/jobs/cleanup-all", response_model=HubJobResponse)
    async def cleanup_all_job():
        from ....core.request_context import get_request_id

        async def _run_cleanup_all():
            result = await asyncio.to_thread(context.supervisor.cleanup_all)
            await cache.invalidate_caches()
            return result

        job = await context.job_manager.submit(
            "hub.cleanup_all",
            _run_cleanup_all,
            request_id=get_request_id(),
        )
        return job.to_dict()

    @router.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        return await run_control.run_repo(repo_id, once)

    @router.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        return await run_control.stop_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        return await run_control.resume_repo(repo_id, once)

    @router.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        return await run_control.kill_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        return await run_control.init_repo(repo_id)

    @router.post("/hub/repos/{repo_id}/sync-main")
    async def sync_repo_main(repo_id: str):
        return await run_control.sync_main(repo_id)

    listing_router = build_hub_repo_listing_router(context, mount_manager, enricher)
    crud_router = build_hub_repo_crud_router(context, mount_manager, enricher)
    agent_workspace_router = build_hub_agent_workspace_router(context)
    channel_router = build_hub_channel_router(context)
    overlapping_pairs = (
        _route_method_path_pairs(listing_router)
        | _route_method_path_pairs(crud_router)
        | _route_method_path_pairs(agent_workspace_router)
        | _route_method_path_pairs(channel_router)
    )
    _prune_overlapping_routes(router, overlapping_pairs)

    router.include_router(listing_router)
    router.include_router(crud_router)
    router.include_router(agent_workspace_router)
    router.include_router(channel_router)

    return router
