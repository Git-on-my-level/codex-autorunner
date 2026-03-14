from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .....core.chat_bindings import (
    active_chat_binding_counts,
    active_chat_binding_counts_by_source,
)
from .....core.freshness import (
    iso_now,
    resolve_stale_threshold_seconds,
    summarize_section_freshness,
)
from .....core.logging_utils import safe_log
from .....core.request_context import get_request_id

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


class HubRepoListingService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._enricher = enricher

    def _active_chat_binding_counts(self) -> dict[str, int]:
        try:
            return active_chat_binding_counts(
                hub_root=self._context.config.root,
                raw_config=self._context.config.raw,
            )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub active chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _active_chat_binding_counts_by_source(self) -> dict[str, dict[str, int]]:
        try:
            return active_chat_binding_counts_by_source(
                hub_root=self._context.config.root,
                raw_config=self._context.config.raw,
            )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub source chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    async def list_repos(self) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub list_repos")
        snapshots = await asyncio.to_thread(self._context.supervisor.list_repos)
        agent_workspace_snapshots = await asyncio.to_thread(
            self._context.supervisor.list_agent_workspaces
        )
        chat_binding_counts = await asyncio.to_thread(self._active_chat_binding_counts)
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        await self._mount_manager.refresh_mounts(snapshots)
        repos = [
            self._enricher.enrich_repo(
                snap, chat_binding_counts, chat_binding_counts_by_source
            )
            for snap in snapshots
        ]
        agent_workspaces = [
            workspace.to_dict(self._context.config.root)
            for workspace in agent_workspace_snapshots
        ]
        generated_at = iso_now()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        return {
            "generated_at": generated_at,
            "last_scan_at": self._context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": self._context.supervisor.state.pinned_parent_repo_ids,
            "freshness": {
                "schema_version": 1,
                "generated_at": generated_at,
                "stale_threshold_seconds": stale_threshold_seconds,
                "sections": {
                    "repos": summarize_section_freshness(
                        repos,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda item: (
                            (item.get("canonical_state_v1") or {}).get("freshness")
                            if isinstance(item, dict)
                            else None
                        ),
                    ),
                    "agent_workspaces": summarize_section_freshness(
                        agent_workspaces,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda _item: None,
                    ),
                },
            },
            "repos": repos,
            "agent_workspaces": agent_workspaces,
        }

    async def scan_repos(self) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(self._context.supervisor.scan)
        agent_workspace_snapshots = await asyncio.to_thread(
            self._context.supervisor.list_agent_workspaces, use_cache=False
        )
        chat_binding_counts = await asyncio.to_thread(self._active_chat_binding_counts)
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        await self._mount_manager.refresh_mounts(snapshots)
        repos = [
            self._enricher.enrich_repo(
                snap, chat_binding_counts, chat_binding_counts_by_source
            )
            for snap in snapshots
        ]
        agent_workspaces = [
            workspace.to_dict(self._context.config.root)
            for workspace in agent_workspace_snapshots
        ]
        generated_at = iso_now()
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        return {
            "generated_at": generated_at,
            "last_scan_at": self._context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": self._context.supervisor.state.pinned_parent_repo_ids,
            "freshness": {
                "schema_version": 1,
                "generated_at": generated_at,
                "stale_threshold_seconds": stale_threshold_seconds,
                "sections": {
                    "repos": summarize_section_freshness(
                        repos,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda item: (
                            (item.get("canonical_state_v1") or {}).get("freshness")
                            if isinstance(item, dict)
                            else None
                        ),
                    ),
                    "agent_workspaces": summarize_section_freshness(
                        agent_workspaces,
                        generated_at=generated_at,
                        stale_threshold_seconds=stale_threshold_seconds,
                        extractor=lambda _item: None,
                    ),
                },
            },
            "repos": repos,
            "agent_workspaces": agent_workspaces,
        }

    async def scan_repos_job(self) -> dict[str, Any]:
        async def _run_scan():
            snapshots = await asyncio.to_thread(self._context.supervisor.scan)
            await self._mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await self._context.job_manager.submit(
            "hub.scan_repos", _run_scan, request_id=get_request_id()
        )
        return job.to_dict()


def build_hub_repo_listing_router(
    context: HubAppContext,
    mount_manager: HubMountManager,
    enricher: HubRepoEnricher,
) -> APIRouter:
    from fastapi import APIRouter

    from ...schemas import HubJobResponse

    router = APIRouter()
    listing_service = HubRepoListingService(context, mount_manager, enricher)

    @router.get("/hub/repos")
    async def list_repos():
        return await listing_service.list_repos()

    @router.post("/hub/repos/scan")
    async def scan_repos():
        return await listing_service.scan_repos()

    @router.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        return await listing_service.scan_repos_job()

    return router
