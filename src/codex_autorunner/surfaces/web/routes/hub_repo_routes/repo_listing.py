from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from .....core.chat_bindings import active_chat_binding_counts_by_source
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

REPO_LISTING_SECTIONS = frozenset({"repos", "agent_workspaces", "freshness"})
_REPO_LISTING_RESPONSE_CACHE_TTL_SECONDS = 20.0


@dataclass(frozen=True)
class _RepoListingCacheEntry:
    fingerprint: tuple[Any, ...]
    expires_at: float
    payload: dict[str, Any]


def _monotonic() -> float:
    return time.monotonic()


def _path_stat_fingerprint(path: Path) -> tuple[bool, Optional[int], Optional[int]]:
    try:
        stat = path.stat()
    except OSError:
        return (False, None, None)
    return (True, int(stat.st_mtime_ns), int(stat.st_size))


def normalize_repo_listing_sections(raw: Optional[str]) -> set[str]:
    if raw is None:
        return set(REPO_LISTING_SECTIONS)
    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not requested:
        return set(REPO_LISTING_SECTIONS)
    invalid = requested - REPO_LISTING_SECTIONS
    if invalid:
        invalid_text = ", ".join(sorted(invalid))
        allowed_text = ", ".join(sorted(REPO_LISTING_SECTIONS))
        raise ValueError(
            f"Unsupported hub repo sections: {invalid_text}. Allowed: {allowed_text}."
        )
    return requested


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
        self._response_cache: dict[tuple[str, ...], _RepoListingCacheEntry] = {}
        self._response_cache_lock = threading.Lock()
        self._response_refresh_tasks: dict[tuple[str, ...], asyncio.Task[None]] = {}
        self._response_refresh_tasks_lock = threading.Lock()

    async def _enrich_repos(
        self,
        snapshots: list[Any],
        chat_binding_counts: dict[str, int],
        chat_binding_counts_by_source: dict[str, dict[str, int]],
    ) -> list[dict[str, Any]]:
        tasks = [
            asyncio.to_thread(
                self._enricher.enrich_repo,
                snap,
                chat_binding_counts,
                chat_binding_counts_by_source,
            )
            for snap in snapshots
        ]
        return cast(list[dict[str, Any]], await asyncio.gather(*tasks))

    def _active_chat_binding_counts_by_source(self) -> dict[str, dict[str, int]]:
        try:
            return active_chat_binding_counts_by_source(
                hub_root=self._context.config.root,
                raw_config=self._context.config.raw,
            )
        except (
            Exception
        ) as exc:  # intentional: chat binding lookup failure is non-critical
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub source chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _response_cache_fingerprint(self, *, requested: set[str]) -> tuple[Any, ...]:
        supervisor_state = getattr(self._context.supervisor, "state", None)
        pinned_parent_repo_ids = (
            getattr(supervisor_state, "pinned_parent_repo_ids", []) or []
        )
        manifest_path = getattr(self._context.config, "manifest_path", None)
        return (
            tuple(sorted(requested)),
            getattr(supervisor_state, "last_scan_at", None),
            tuple(pinned_parent_repo_ids),
            (
                _path_stat_fingerprint(manifest_path)
                if isinstance(manifest_path, Path)
                else None
            ),
        )

    def _store_response_cache(
        self,
        *,
        cache_key: tuple[str, ...],
        fingerprint: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        with self._response_cache_lock:
            self._response_cache[cache_key] = _RepoListingCacheEntry(
                fingerprint=fingerprint,
                expires_at=_monotonic() + _REPO_LISTING_RESPONSE_CACHE_TTL_SECONDS,
                payload=copy.deepcopy(payload),
            )

    def _schedule_response_refresh(
        self,
        *,
        cache_key: tuple[str, ...],
        fingerprint: tuple[Any, ...],
        requested: set[str],
    ) -> None:
        with self._response_refresh_tasks_lock:
            task = self._response_refresh_tasks.get(cache_key)
            if task is not None and not task.done():
                return

            async def _refresh() -> None:
                payload = await self._build_listing_payload(sections=requested)
                self._store_response_cache(
                    cache_key=cache_key,
                    fingerprint=fingerprint,
                    payload=payload,
                )

            task = asyncio.create_task(_refresh())
            self._response_refresh_tasks[cache_key] = task

        def _cleanup(done_task: asyncio.Task[None]) -> None:
            with self._response_refresh_tasks_lock:
                current = self._response_refresh_tasks.get(cache_key)
                if current is done_task:
                    self._response_refresh_tasks.pop(cache_key, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # intentional: background refresh is best-effort
                safe_log(
                    self._context.logger,
                    logging.WARNING,
                    "Hub list_repos background refresh failed",
                    exc=exc,
                )

        task.add_done_callback(_cleanup)

    async def _build_listing_payload(
        self, *, sections: Optional[set[str]] = None
    ) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub list_repos")
        requested = set(sections or REPO_LISTING_SECTIONS)
        needs_repos = bool(requested & {"repos", "freshness"})
        needs_agent_workspaces = bool(requested & {"agent_workspaces", "freshness"})
        snapshots = []
        repos: list[dict[str, Any]] = []
        agent_workspaces: list[dict[str, Any]] = []

        if needs_repos:
            tasks = [
                asyncio.to_thread(self._context.supervisor.list_repos),
                asyncio.to_thread(self._active_chat_binding_counts_by_source),
            ]
            if needs_agent_workspaces:
                tasks.append(
                    asyncio.to_thread(self._context.supervisor.list_agent_workspaces)
                )
            results = await asyncio.gather(*tasks)
            snapshots = results[0]
            chat_binding_counts_by_source = results[1]
            chat_binding_counts = {
                repo_id: sum(source_counts.values())
                for repo_id, source_counts in chat_binding_counts_by_source.items()
            }
            await self._mount_manager.refresh_mounts(snapshots)
            repos = await self._enrich_repos(
                snapshots,
                chat_binding_counts,
                chat_binding_counts_by_source,
            )
            if needs_agent_workspaces:
                agent_workspace_snapshots = results[2]
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspace_snapshots
                ]
        elif needs_agent_workspaces:
            agent_workspace_snapshots = await asyncio.to_thread(
                self._context.supervisor.list_agent_workspaces
            )
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
        payload = {
            "generated_at": generated_at,
            "last_scan_at": self._context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": self._context.supervisor.state.pinned_parent_repo_ids,
        }
        if "freshness" in requested:
            payload["freshness"] = {
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
            }
        if "repos" in requested:
            payload["repos"] = repos
        if "agent_workspaces" in requested:
            payload["agent_workspaces"] = agent_workspaces
        return payload

    async def list_repos(
        self, *, sections: Optional[set[str]] = None
    ) -> dict[str, Any]:
        requested = set(sections or REPO_LISTING_SECTIONS)
        cache_key = tuple(sorted(requested))
        fingerprint = self._response_cache_fingerprint(requested=requested)
        now = _monotonic()
        with self._response_cache_lock:
            cached = self._response_cache.get(cache_key)
        if cached is not None and cached.fingerprint == fingerprint:
            if cached.expires_at > now:
                return copy.deepcopy(cached.payload)
            self._schedule_response_refresh(
                cache_key=cache_key,
                fingerprint=fingerprint,
                requested=requested,
            )
            return copy.deepcopy(cached.payload)

        payload = await self._build_listing_payload(sections=requested)
        self._store_response_cache(
            cache_key=cache_key,
            fingerprint=fingerprint,
            payload=payload,
        )
        return payload

    async def scan_repos(self) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(self._context.supervisor.scan)
        agent_workspace_snapshots = await asyncio.to_thread(
            self._context.supervisor.list_agent_workspaces, use_cache=False
        )
        chat_binding_counts_by_source = await asyncio.to_thread(
            self._active_chat_binding_counts_by_source
        )
        chat_binding_counts = {
            repo_id: sum(source_counts.values())
            for repo_id, source_counts in chat_binding_counts_by_source.items()
        }
        await self._mount_manager.refresh_mounts(snapshots)
        repos = await self._enrich_repos(
            snapshots,
            chat_binding_counts,
            chat_binding_counts_by_source,
        )
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
        payload = {
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
        self._store_response_cache(
            cache_key=tuple(sorted(REPO_LISTING_SECTIONS)),
            fingerprint=self._response_cache_fingerprint(
                requested=set(REPO_LISTING_SECTIONS)
            ),
            payload=payload,
        )
        return payload

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
    from fastapi import APIRouter, HTTPException

    from ...schemas import HubJobResponse

    router = APIRouter()
    listing_service = HubRepoListingService(context, mount_manager, enricher)

    @router.get("/hub/repos")
    async def list_repos(sections: Optional[str] = None):
        try:
            requested_sections = normalize_repo_listing_sections(sections)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await listing_service.list_repos(sections=requested_sections)

    @router.post("/hub/repos/scan")
    async def scan_repos():
        return await listing_service.scan_repos()

    @router.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        return await listing_service.scan_repos_job()

    return router
