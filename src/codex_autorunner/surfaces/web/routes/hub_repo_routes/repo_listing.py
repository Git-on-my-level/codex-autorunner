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
from .....core.pma_thread_store_bootstrap import default_pma_threads_db_path
from .....core.request_context import get_request_id
from .....core.state_roots import resolve_hub_orchestration_db_path
from .....integrations.discord.config import DEFAULT_STATE_FILE as DISCORD_STATE_FILE
from .....integrations.telegram.config import DEFAULT_STATE_FILE as TELEGRAM_STATE_FILE

if TYPE_CHECKING:
    from fastapi import APIRouter

    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher

REPO_LISTING_SECTIONS = frozenset({"repos", "agent_workspaces", "freshness"})
_REPO_LISTING_RESPONSE_CACHE_TTL_SECONDS = 20.0
_HUB_LISTING_PROJECTION_NAMESPACE = "hub_listing_v1"
_HUB_LISTING_PROJECTION_MAX_AGE_SECONDS = 60.0


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

    def _projection_store(self):
        return getattr(self._context, "projection_store", None)

    def _resolve_state_path(self, section: str, default_state_file: str) -> Path:
        raw = (
            self._context.config.raw
            if isinstance(self._context.config.raw, dict)
            else {}
        )
        section_cfg = raw.get(section)
        state_file = default_state_file
        if isinstance(section_cfg, dict):
            candidate = section_cfg.get("state_file")
            if isinstance(candidate, str) and candidate.strip():
                state_file = candidate.strip()
        path = Path(state_file)
        if not path.is_absolute():
            path = (self._context.config.root / path).resolve()
        return path

    def _chat_binding_counts_fingerprint(self) -> tuple[Any, ...]:
        manifest_path = getattr(self._context.config, "manifest_path", None)
        return (
            (
                _path_stat_fingerprint(manifest_path)
                if isinstance(manifest_path, Path)
                else None
            ),
            _path_stat_fingerprint(
                default_pma_threads_db_path(self._context.config.root)
            ),
            _path_stat_fingerprint(
                resolve_hub_orchestration_db_path(self._context.config.root)
            ),
            _path_stat_fingerprint(
                self._resolve_state_path("discord_bot", DISCORD_STATE_FILE)
            ),
            _path_stat_fingerprint(
                self._resolve_state_path("telegram_bot", TELEGRAM_STATE_FILE)
            ),
        )

    def _repo_runtime_fingerprint(
        self, snapshot, *, stale_threshold_seconds: Optional[int]
    ) -> tuple[Any, ...]:
        fingerprint_fn = getattr(self._enricher, "repo_state_fingerprint", None)
        if callable(fingerprint_fn):
            return cast(
                tuple[Any, ...],
                fingerprint_fn(
                    snapshot,
                    stale_threshold_seconds=stale_threshold_seconds,
                ),
            )
        repo_root = getattr(snapshot, "path", None)
        return (
            str(getattr(snapshot, "id", "")),
            str(repo_root) if isinstance(repo_root, Path) else str(repo_root),
            bool(getattr(snapshot, "exists_on_disk", False)),
            bool(getattr(snapshot, "initialized", False)),
            getattr(snapshot, "last_run_id", None),
            getattr(snapshot, "last_run_started_at", None),
            getattr(snapshot, "last_run_finished_at", None),
            getattr(snapshot, "last_exit_code", None),
            getattr(snapshot, "runner_pid", None),
            int(stale_threshold_seconds or 0),
        )

    def _listing_fingerprint(
        self,
        *,
        requested: set[str],
        stale_threshold_seconds: int,
        repos: list[Any],
        agent_workspaces: list[Any],
    ) -> tuple[Any, ...]:
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
            tuple(
                self._repo_runtime_fingerprint(
                    snap, stale_threshold_seconds=stale_threshold_seconds
                )
                for snap in repos
            ),
            tuple(
                (
                    workspace.id,
                    workspace.runtime,
                    str(workspace.path),
                    workspace.display_name,
                    workspace.enabled,
                    workspace.exists_on_disk,
                )
                for workspace in agent_workspaces
            ),
        )

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

    def _current_topology_snapshots(
        self, *, needs_repos: bool, needs_agent_workspaces: bool
    ) -> tuple[list[Any], list[Any]]:
        supervisor_state = getattr(self._context.supervisor, "state", None)
        snapshots = list(getattr(supervisor_state, "repos", []) or [])
        agent_workspaces = list(getattr(supervisor_state, "agent_workspaces", []) or [])
        if needs_repos and not snapshots:
            snapshots = list(self._context.supervisor.list_repos())
            supervisor_state = getattr(self._context.supervisor, "state", None)
            agent_workspaces = list(
                getattr(supervisor_state, "agent_workspaces", []) or agent_workspaces
            )
        if needs_agent_workspaces and not agent_workspaces:
            agent_workspaces = list(self._context.supervisor.list_agent_workspaces())
        return snapshots, agent_workspaces

    def _force_list_repos(self) -> list[Any]:
        try:
            return list(self._context.supervisor.list_repos(use_cache=False))
        except TypeError:
            return list(self._context.supervisor.list_repos())

    def _force_list_agent_workspaces(self) -> list[Any]:
        try:
            return list(self._context.supervisor.list_agent_workspaces(use_cache=False))
        except TypeError:
            return list(self._context.supervisor.list_agent_workspaces())

    async def _load_topology_snapshots(
        self,
        *,
        needs_repos: bool,
        needs_agent_workspaces: bool,
        force_refresh: bool,
    ) -> tuple[list[Any], list[Any]]:
        if force_refresh:
            if needs_repos:
                snapshots = await asyncio.to_thread(self._force_list_repos)
                supervisor_state = getattr(self._context.supervisor, "state", None)
                agent_workspaces = list(
                    getattr(supervisor_state, "agent_workspaces", []) or []
                )
                if needs_agent_workspaces and not agent_workspaces:
                    agent_workspaces = await asyncio.to_thread(
                        self._force_list_agent_workspaces
                    )
                return list(snapshots), list(agent_workspaces)
            if needs_agent_workspaces:
                agent_workspaces = await asyncio.to_thread(
                    self._force_list_agent_workspaces
                )
                return [], list(agent_workspaces)
        return self._current_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
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
                needs_repos = bool(requested & {"repos", "freshness"})
                needs_agent_workspaces = bool(
                    requested & {"agent_workspaces", "freshness"}
                )
                snapshots, agent_workspaces = await self._load_topology_snapshots(
                    needs_repos=needs_repos,
                    needs_agent_workspaces=needs_agent_workspaces,
                    force_refresh=True,
                )
                stale_threshold_seconds = resolve_stale_threshold_seconds(
                    getattr(
                        self._context.config.pma,
                        "freshness_stale_threshold_seconds",
                        None,
                    )
                )
                refreshed_fingerprint = self._listing_fingerprint(
                    requested=requested,
                    stale_threshold_seconds=stale_threshold_seconds,
                    repos=snapshots,
                    agent_workspaces=agent_workspaces,
                )
                payload = await self._build_listing_payload(
                    sections=requested,
                    snapshots=snapshots,
                    agent_workspaces=agent_workspaces,
                )
                self._store_response_cache(
                    cache_key=cache_key,
                    fingerprint=refreshed_fingerprint,
                    payload=payload,
                )
                projection_store = self._projection_store()
                if projection_store is not None:
                    try:
                        projection_store.set_cache(
                            f"hub_listing:{','.join(cache_key)}",
                            refreshed_fingerprint,
                            payload,
                            namespace=_HUB_LISTING_PROJECTION_NAMESPACE,
                        )
                    except Exception:
                        pass

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
        self,
        *,
        sections: Optional[set[str]] = None,
        snapshots: Optional[list[Any]] = None,
        agent_workspaces: Optional[list[Any]] = None,
    ) -> dict[str, Any]:
        safe_log(self._context.logger, logging.INFO, "Hub list_repos")
        requested = set(sections or REPO_LISTING_SECTIONS)
        needs_repos = bool(requested & {"repos", "freshness"})
        needs_agent_workspaces = bool(requested & {"agent_workspaces", "freshness"})
        snapshots = list(snapshots or [])
        repos: list[dict[str, Any]] = []
        agent_workspaces = list(agent_workspaces or [])

        if needs_repos:
            if not snapshots or (needs_agent_workspaces and not agent_workspaces):
                snapshots, agent_workspaces = self._current_topology_snapshots(
                    needs_repos=True,
                    needs_agent_workspaces=needs_agent_workspaces,
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
            if needs_agent_workspaces:
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspaces
                ]
        elif needs_agent_workspaces:
            if not agent_workspaces:
                _, agent_workspace_snaps = self._current_topology_snapshots(
                    needs_repos=False,
                    needs_agent_workspaces=True,
                )
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspace_snaps
                ]
            else:
                agent_workspaces = [
                    workspace.to_dict(self._context.config.root)
                    for workspace in agent_workspaces
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
        durable_cache_key = f"hub_listing:{','.join(cache_key)}"
        needs_repos = bool(requested & {"repos", "freshness"})
        needs_agent_workspaces = bool(requested & {"agent_workspaces", "freshness"})
        snapshots, agent_workspaces = await self._load_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
            force_refresh=False,
        )
        supervisor_state = getattr(self._context.supervisor, "state", None)
        if (
            needs_repos
            and not snapshots
            and getattr(supervisor_state, "last_scan_at", None) is None
        ):
            snapshots = await asyncio.to_thread(self._context.supervisor.scan)
            if needs_agent_workspaces:
                agent_workspaces = await asyncio.to_thread(
                    self._context.supervisor.list_agent_workspaces,
                    use_cache=False,
                )
        stale_threshold_seconds = resolve_stale_threshold_seconds(
            getattr(
                self._context.config.pma,
                "freshness_stale_threshold_seconds",
                None,
            )
        )
        fingerprint = self._listing_fingerprint(
            requested=requested,
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspaces,
        )
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
        snapshots, agent_workspaces = await self._load_topology_snapshots(
            needs_repos=needs_repos,
            needs_agent_workspaces=needs_agent_workspaces,
            force_refresh=True,
        )
        fingerprint = self._listing_fingerprint(
            requested=requested,
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspaces,
        )
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                cached = projection_store.get_cache(
                    durable_cache_key,
                    fingerprint,
                    max_age_seconds=_HUB_LISTING_PROJECTION_MAX_AGE_SECONDS,
                    namespace=_HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                cached = None
            if cached is not None:
                payload = cast(dict[str, Any], cached)
                self._store_response_cache(
                    cache_key=cache_key,
                    fingerprint=fingerprint,
                    payload=payload,
                )
                return copy.deepcopy(payload)

        payload = await self._build_listing_payload(
            sections=requested,
            snapshots=snapshots,
            agent_workspaces=agent_workspaces,
        )
        self._store_response_cache(
            cache_key=cache_key,
            fingerprint=fingerprint,
            payload=payload,
        )
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    durable_cache_key,
                    fingerprint,
                    payload,
                    namespace=_HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
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
        listing_fingerprint = self._listing_fingerprint(
            requested=set(REPO_LISTING_SECTIONS),
            stale_threshold_seconds=stale_threshold_seconds,
            repos=snapshots,
            agent_workspaces=agent_workspace_snapshots,
        )
        self._store_response_cache(
            cache_key=tuple(sorted(REPO_LISTING_SECTIONS)),
            fingerprint=listing_fingerprint,
            payload=payload,
        )
        projection_store = self._projection_store()
        if projection_store is not None:
            try:
                projection_store.set_cache(
                    "hub_listing:agent_workspaces,freshness,repos",
                    listing_fingerprint,
                    payload,
                    namespace=_HUB_LISTING_PROJECTION_NAMESPACE,
                )
            except Exception:
                pass
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
