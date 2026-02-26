from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from starlette.routing import Mount
from starlette.types import ASGIApp

from ....core.chat_bound_worktrees import is_chat_bound_worktree_identity
from ....core.config import ConfigError
from ....core.logging_utils import safe_log
from ....core.pma_context import get_latest_ticket_flow_run_state
from ....core.pma_thread_store import PmaThreadStore, default_pma_threads_db_path
from ....core.request_context import get_request_id
from ....core.runtime import LockError
from ....core.ticket_flow_summary import (
    build_ticket_flow_display,
    build_ticket_flow_summary,
)
from ..app_state import HubAppContext
from ..schemas import (
    HubArchiveWorktreeRequest,
    HubArchiveWorktreeResponse,
    HubCleanupWorktreeRequest,
    HubCreateRepoRequest,
    HubCreateWorktreeRequest,
    HubJobResponse,
    HubPinRepoRequest,
    HubRemoveRepoRequest,
    RunControlRequest,
)


class HubMountManager:
    def __init__(
        self,
        app: FastAPI,
        context: HubAppContext,
        build_repo_app: Callable[[Path], ASGIApp],
    ) -> None:
        self.app = app
        self.context = context
        self._build_repo_app = build_repo_app

        self._mounted_repos: set[str] = set()
        self._mount_errors: dict[str, str] = {}
        self._repo_apps: dict[str, ASGIApp] = {}
        self._repo_lifespans: dict[str, object] = {}
        self._mount_order: list[str] = []
        self._mount_lock: Optional[asyncio.Lock] = None

    async def _get_mount_lock(self) -> asyncio.Lock:
        if self._mount_lock is None:
            self._mount_lock = asyncio.Lock()
        return self._mount_lock

    @staticmethod
    def _unwrap_fastapi(sub_app: ASGIApp) -> Optional[FastAPI]:
        current: ASGIApp = sub_app
        while not isinstance(current, FastAPI):
            nested = getattr(current, "app", None)
            if nested is None:
                return None
            current = nested
        return current

    async def _start_repo_lifespan_locked(self, prefix: str, sub_app: ASGIApp) -> None:
        if prefix in self._repo_lifespans:
            return
        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is None:
            return
        try:
            ctx = fastapi_app.router.lifespan_context(fastapi_app)
            await ctx.__aenter__()
            self._repo_lifespans[prefix] = ctx
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan entered for {prefix}",
            )
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning(
                    "Repo lifespan failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan failure for {prefix}",
                    exc=exc2,
                )
            await self._unmount_repo_locked(prefix)

    async def _stop_repo_lifespan_locked(self, prefix: str) -> None:
        ctx = self._repo_lifespans.pop(prefix, None)
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)  # type: ignore[attr-defined]
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan exited for {prefix}",
            )
        except Exception as exc:
            try:
                self.app.state.logger.warning(
                    "Repo lifespan shutdown failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan shutdown failure for {prefix}",
                    exc=exc2,
                )

    def _detach_mount_locked(self, prefix: str) -> None:
        mount_path = f"/repos/{prefix}"
        self.app.router.routes = [
            route
            for route in self.app.router.routes
            if not (isinstance(route, Mount) and route.path == mount_path)
        ]
        self._mounted_repos.discard(prefix)
        self._repo_apps.pop(prefix, None)
        if prefix in self._mount_order:
            self._mount_order.remove(prefix)

    async def _unmount_repo_locked(self, prefix: str) -> None:
        await self._stop_repo_lifespan_locked(prefix)
        self._detach_mount_locked(prefix)

    def _mount_repo_sync(self, prefix: str, repo_path: Path) -> bool:
        if prefix in self._mounted_repos:
            return True
        if prefix in self._mount_errors:
            return False
        try:
            sub_app = self._build_repo_app(repo_path)
        except ConfigError as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False

        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is not None:
            fastapi_app.state.repo_id = prefix

        self.app.mount(f"/repos/{prefix}", sub_app)
        self._mounted_repos.add(prefix)
        self._repo_apps[prefix] = sub_app
        if prefix not in self._mount_order:
            self._mount_order.append(prefix)
        self._mount_errors.pop(prefix, None)
        return True

    def mount_initial(self, snapshots: Iterable[Any]) -> None:
        for snapshot in snapshots:
            if getattr(snapshot, "initialized", False) and getattr(
                snapshot, "exists_on_disk", False
            ):
                self._mount_repo_sync(snapshot.id, snapshot.path)

    async def refresh_mounts(
        self, snapshots: Iterable[Any], *, full_refresh: bool = True
    ):
        desired = {
            snapshot.id
            for snapshot in snapshots
            if getattr(snapshot, "initialized", False)
            and getattr(snapshot, "exists_on_disk", False)
        }
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            if full_refresh:
                for prefix in list(self._mounted_repos):
                    if prefix not in desired:
                        await self._unmount_repo_locked(prefix)
                for prefix in list(self._mount_errors):
                    if prefix not in desired:
                        self._mount_errors.pop(prefix, None)

            for snapshot in snapshots:
                if snapshot.id not in desired:
                    continue
                if (
                    snapshot.id in self._mounted_repos
                    or snapshot.id in self._mount_errors
                ):
                    continue
                if not self._mount_repo_sync(snapshot.id, snapshot.path):
                    continue
                fastapi_app = self._unwrap_fastapi(self._repo_apps[snapshot.id])
                if fastapi_app is not None:
                    fastapi_app.state.repo_id = snapshot.id
                if self.app.state.hub_started:
                    await self._start_repo_lifespan_locked(
                        snapshot.id, self._repo_apps[snapshot.id]
                    )

    async def start_repo_lifespans(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(self._mount_order):
                sub_app = self._repo_apps.get(prefix)
                if sub_app is not None:
                    await self._start_repo_lifespan_locked(prefix, sub_app)

    async def stop_repo_mounts(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(reversed(self._mount_order)):
                await self._stop_repo_lifespan_locked(prefix)
            for prefix in list(self._mounted_repos):
                self._detach_mount_locked(prefix)

    def add_mount_info(self, repo_dict: dict) -> dict:
        repo_id = repo_dict.get("id")
        if repo_id in self._mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = self._mount_errors[repo_id]
        elif repo_id in self._mounted_repos:
            repo_dict["mounted"] = True
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        else:
            repo_dict["mounted"] = False
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        return repo_dict


def build_hub_repo_routes(
    context: HubAppContext,
    mount_manager: HubMountManager,
) -> APIRouter:
    router = APIRouter()

    def _active_chat_binding_counts() -> dict[str, int]:
        db_path = default_pma_threads_db_path(context.config.root)
        if not db_path.exists():
            return {}
        try:
            store = PmaThreadStore(context.config.root)
            return store.count_threads_by_repo(status="active")
        except Exception as exc:
            safe_log(
                context.logger,
                logging.WARNING,
                "Hub active chat-bound worktree lookup failed",
                exc=exc,
            )
            return {}

    def _enrich_repo(
        snapshot, chat_binding_counts: Optional[dict[str, int]] = None
    ) -> dict:
        repo_dict = snapshot.to_dict(context.config.root)
        repo_dict = mount_manager.add_mount_info(repo_dict)
        binding_count = int((chat_binding_counts or {}).get(snapshot.id, 0))
        identity_chat_bound = is_chat_bound_worktree_identity(
            branch=snapshot.branch,
            repo_id=snapshot.id,
            source_path=snapshot.path,
        )
        repo_dict["chat_bound"] = binding_count > 0 or identity_chat_bound
        repo_dict["chat_bound_thread_count"] = binding_count
        if snapshot.initialized and snapshot.exists_on_disk:
            ticket_flow = _get_ticket_flow_summary(snapshot.path)
            repo_dict["ticket_flow"] = ticket_flow
            if isinstance(ticket_flow, dict):
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=(
                        str(ticket_flow.get("status"))
                        if ticket_flow.get("status") is not None
                        else None
                    ),
                    done_count=int(ticket_flow.get("done_count") or 0),
                    total_count=int(ticket_flow.get("total_count") or 0),
                    run_id=(
                        str(ticket_flow.get("run_id"))
                        if ticket_flow.get("run_id")
                        else None
                    ),
                )
            else:
                repo_dict["ticket_flow_display"] = build_ticket_flow_display(
                    status=None,
                    done_count=0,
                    total_count=0,
                    run_id=None,
                )
            repo_dict["run_state"] = get_latest_ticket_flow_run_state(
                snapshot.path, snapshot.id
            )
        else:
            repo_dict["ticket_flow"] = None
            repo_dict["ticket_flow_display"] = None
            repo_dict["run_state"] = None
        return repo_dict

    def _get_ticket_flow_summary(repo_path: Path) -> Optional[dict]:
        return build_ticket_flow_summary(repo_path, include_failure=True)

    @router.get("/hub/repos")
    async def list_repos():
        safe_log(context.logger, logging.INFO, "Hub list_repos")
        snapshots = await asyncio.to_thread(context.supervisor.list_repos)
        chat_binding_counts = await asyncio.to_thread(_active_chat_binding_counts)
        await mount_manager.refresh_mounts(snapshots)
        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": context.supervisor.state.pinned_parent_repo_ids,
            "repos": [_enrich_repo(snap, chat_binding_counts) for snap in snapshots],
        }

    @router.post("/hub/repos/scan")
    async def scan_repos():
        safe_log(context.logger, logging.INFO, "Hub scan_repos")
        snapshots = await asyncio.to_thread(context.supervisor.scan)
        chat_binding_counts = await asyncio.to_thread(_active_chat_binding_counts)
        await mount_manager.refresh_mounts(snapshots)

        return {
            "last_scan_at": context.supervisor.state.last_scan_at,
            "pinned_parent_repo_ids": context.supervisor.state.pinned_parent_repo_ids,
            "repos": [_enrich_repo(snap, chat_binding_counts) for snap in snapshots],
        }

    @router.post("/hub/jobs/scan", response_model=HubJobResponse)
    async def scan_repos_job():
        async def _run_scan():
            snapshots = await asyncio.to_thread(context.supervisor.scan)
            await mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.scan_repos", _run_scan, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/repos")
    async def create_repo(payload: HubCreateRepoRequest):
        git_url = payload.git_url
        repo_id = payload.repo_id
        if not repo_id and not git_url:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.path
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = payload.git_init
        force = payload.force
        safe_log(
            context.logger,
            logging.INFO,
            "Hub create repo id=%s path=%s git_init=%s force=%s git_url=%s"
            % (repo_id, repo_path_val, git_init, force, bool(git_url)),
        )
        try:
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/jobs/repos", response_model=HubJobResponse)
    async def create_repo_job(payload: HubCreateRepoRequest):
        async def _run_create_repo():
            git_url = payload.git_url
            repo_id = payload.repo_id
            if not repo_id and not git_url:
                raise ValueError("Missing repo id")
            repo_path_val = payload.path
            repo_path = Path(repo_path_val) if repo_path_val else None
            git_init = payload.git_init
            force = payload.force
            if git_url:
                snapshot = await asyncio.to_thread(
                    context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
            await mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return _enrich_repo(snapshot)

        job = await context.job_manager.submit(
            "hub.create_repo", _run_create_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/repos/{repo_id}/worktree-setup")
    async def set_worktree_setup(repo_id: str, payload: dict[str, Any]):
        commands_raw = payload.get("commands") if isinstance(payload, dict) else []
        if not isinstance(commands_raw, list):
            raise HTTPException(status_code=400, detail="commands must be a list")
        commands = [str(item) for item in commands_raw if str(item).strip()]
        safe_log(
            context.logger,
            logging.INFO,
            "Hub set worktree setup repo=%s commands=%d" % (repo_id, len(commands)),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.set_worktree_setup_commands,
                repo_id,
                commands,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/pin")
    async def pin_parent_repo(
        repo_id: str, payload: Optional[HubPinRepoRequest] = None
    ):
        requested = payload.pinned if payload else True
        safe_log(
            context.logger,
            logging.INFO,
            "Hub pin parent repo=%s pinned=%s" % (repo_id, requested),
        )
        try:
            pinned_parent_repo_ids = await asyncio.to_thread(
                context.supervisor.set_parent_repo_pinned,
                repo_id,
                requested,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "repo_id": repo_id,
            "pinned": requested,
            "pinned_parent_repo_ids": pinned_parent_repo_ids,
        }

    @router.get("/hub/repos/{repo_id}/remove-check")
    async def remove_repo_check(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub remove-check {repo_id}")
        try:
            return await asyncio.to_thread(
                context.supervisor.check_repo_removal, repo_id
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/hub/repos/{repo_id}/remove")
    async def remove_repo(repo_id: str, payload: Optional[HubRemoveRepoRequest] = None):
        payload = payload or HubRemoveRepoRequest()
        force = payload.force
        delete_dir = payload.delete_dir
        delete_worktrees = payload.delete_worktrees
        safe_log(
            context.logger,
            logging.INFO,
            "Hub remove repo id=%s force=%s delete_dir=%s delete_worktrees=%s"
            % (repo_id, force, delete_dir, delete_worktrees),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=force,
                delete_dir=delete_dir,
                delete_worktrees=delete_worktrees,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshots = await asyncio.to_thread(
            context.supervisor.list_repos, use_cache=False
        )
        await mount_manager.refresh_mounts(snapshots)
        return {"status": "ok"}

    @router.post("/hub/jobs/repos/{repo_id}/remove", response_model=HubJobResponse)
    async def remove_repo_job(
        repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ):
        payload = payload or HubRemoveRepoRequest()

        async def _run_remove_repo():
            await asyncio.to_thread(
                context.supervisor.remove_repo,
                repo_id,
                force=payload.force,
                delete_dir=payload.delete_dir,
                delete_worktrees=payload.delete_worktrees,
            )
            snapshots = await asyncio.to_thread(
                context.supervisor.list_repos, use_cache=False
            )
            await mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.remove_repo", _run_remove_repo, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/create")
    async def create_worktree(payload: HubCreateWorktreeRequest):
        base_repo_id = payload.base_repo_id
        branch = payload.branch
        force = payload.force
        start_point = payload.start_point
        safe_log(
            context.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s start_point=%s"
            % (base_repo_id, branch, force, start_point),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(base_repo_id),
                branch=str(branch),
                force=force,
                start_point=str(start_point) if start_point else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/jobs/worktrees/create", response_model=HubJobResponse)
    async def create_worktree_job(payload: HubCreateWorktreeRequest):
        async def _run_create_worktree():
            snapshot = await asyncio.to_thread(
                context.supervisor.create_worktree,
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
                start_point=str(payload.start_point) if payload.start_point else None,
            )
            await mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return _enrich_repo(snapshot)

        job = await context.job_manager.submit(
            "hub.create_worktree", _run_create_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/cleanup")
    async def cleanup_worktree(payload: HubCleanupWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        delete_branch = payload.delete_branch
        delete_remote = payload.delete_remote
        archive = payload.archive
        force = payload.force
        force_archive = payload.force_archive
        archive_note = payload.archive_note
        safe_log(
            context.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s archive=%s force=%s force_archive=%s"
            % (
                worktree_repo_id,
                delete_branch,
                delete_remote,
                archive,
                force,
                force_archive,
            ),
        )
        try:
            await asyncio.to_thread(
                context.supervisor.cleanup_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                archive=archive,
                force=force,
                force_archive=force_archive,
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok"}

    @router.post("/hub/jobs/worktrees/cleanup", response_model=HubJobResponse)
    async def cleanup_worktree_job(payload: HubCleanupWorktreeRequest):
        def _run_cleanup_worktree():
            context.supervisor.cleanup_worktree(
                worktree_repo_id=str(payload.worktree_repo_id),
                delete_branch=payload.delete_branch,
                delete_remote=payload.delete_remote,
                archive=payload.archive,
                force=payload.force,
                force_archive=payload.force_archive,
                archive_note=payload.archive_note,
            )
            return {"status": "ok"}

        job = await context.job_manager.submit(
            "hub.cleanup_worktree", _run_cleanup_worktree, request_id=get_request_id()
        )
        return job.to_dict()

    @router.post("/hub/worktrees/archive", response_model=HubArchiveWorktreeResponse)
    async def archive_worktree(payload: HubArchiveWorktreeRequest):
        worktree_repo_id = payload.worktree_repo_id
        archive_note = payload.archive_note
        safe_log(
            context.logger,
            logging.INFO,
            "Hub archive worktree id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                context.supervisor.archive_worktree,
                worktree_repo_id=str(worktree_repo_id),
                archive_note=archive_note,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @router.post("/hub/repos/{repo_id}/run")
    async def run_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            context.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.run_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/stop")
    async def stop_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.stop_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/resume")
    async def resume_repo(repo_id: str, payload: Optional[RunControlRequest] = None):
        once = payload.once if payload else False
        safe_log(
            context.logger,
            logging.INFO,
            "Hub resume %s once=%s" % (repo_id, once),
        )
        try:
            snapshot = await asyncio.to_thread(
                context.supervisor.resume_repo, repo_id, once=once
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/kill")
    async def kill_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.kill_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/init")
    async def init_repo(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.init_repo, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    @router.post("/hub/repos/{repo_id}/sync-main")
    async def sync_repo_main(repo_id: str):
        safe_log(context.logger, logging.INFO, f"Hub sync main {repo_id}")
        try:
            snapshot = await asyncio.to_thread(context.supervisor.sync_main, repo_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return _enrich_repo(snapshot)

    return router
