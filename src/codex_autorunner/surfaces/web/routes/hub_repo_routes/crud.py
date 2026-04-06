from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional

from fastapi import APIRouter, Body

from .....core.force_attestation import FORCE_ATTESTATION_REQUIRED_PHRASE
from ...schemas import (
    HubCreateRepoRequest,
    HubJobResponse,
    HubPinRepoRequest,
    HubRemoveRepoRequest,
)

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


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


class HubRepoCrudService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._enricher = enricher

    async def create_repo(self, payload: HubCreateRepoRequest) -> dict[str, Any]:
        from fastapi import HTTPException

        from .....core.logging_utils import safe_log

        git_url = payload.git_url
        repo_id = payload.repo_id
        if not repo_id and not git_url:
            raise HTTPException(status_code=400, detail="Missing repo id")
        repo_path_val = payload.path
        repo_path = Path(repo_path_val) if repo_path_val else None
        git_init = payload.git_init
        force = payload.force
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub create repo id=%s path=%s git_init=%s force=%s git_url=%s"
            % (repo_id, repo_path_val, git_init, force, bool(git_url)),
        )
        try:
            if git_url:
                snapshot = await asyncio.to_thread(
                    self._context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    self._context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return self._enricher.enrich_repo(snapshot)

    async def create_repo_job(self, payload: HubCreateRepoRequest) -> dict[str, Any]:
        from .....core.request_context import get_request_id

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
                    self._context.supervisor.clone_repo,
                    git_url=str(git_url),
                    repo_id=str(repo_id) if repo_id else None,
                    repo_path=repo_path,
                    force=force,
                )
            else:
                snapshot = await asyncio.to_thread(
                    self._context.supervisor.create_repo,
                    str(repo_id),
                    repo_path=repo_path,
                    git_init=git_init,
                    force=force,
                )
            await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return self._enricher.enrich_repo(snapshot)

        job = await self._context.job_manager.submit(
            "hub.create_repo", _run_create_repo, request_id=get_request_id()
        )
        return job.to_dict()

    async def set_worktree_setup(
        self, repo_id: str, payload: Annotated[Any, Body()] = None
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        from .....core.logging_utils import safe_log

        if isinstance(payload, dict):
            commands_raw = payload.get("commands", [])
        elif isinstance(payload, list):
            commands_raw = payload
        elif payload is None:
            commands_raw = []
        else:
            raise HTTPException(
                status_code=400,
                detail="body must be an object with commands or a commands array",
            )
        if not isinstance(commands_raw, list):
            raise HTTPException(status_code=400, detail="commands must be a list")
        commands = [str(item) for item in commands_raw if str(item).strip()]
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub set worktree setup repo=%s commands=%d" % (repo_id, len(commands)),
        )
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.set_worktree_setup_commands,
                repo_id,
                commands,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return self._enricher.enrich_repo(snapshot)

    async def pin_parent_repo(
        self, repo_id: str, payload: Optional[HubPinRepoRequest] = None
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        from .....core.logging_utils import safe_log

        requested = payload.pinned if payload else True
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub pin parent repo=%s pinned=%s" % (repo_id, requested),
        )
        try:
            pinned_parent_repo_ids = await asyncio.to_thread(
                self._context.supervisor.set_parent_repo_pinned,
                repo_id,
                requested,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "repo_id": repo_id,
            "pinned": requested,
            "pinned_parent_repo_ids": pinned_parent_repo_ids,
        }

    async def remove_repo_check(self, repo_id: str) -> dict[str, Any]:
        from .....core.logging_utils import safe_log

        safe_log(self._context.logger, logging.INFO, f"Hub remove-check {repo_id}")
        try:
            return await asyncio.to_thread(
                self._context.supervisor.check_repo_removal, repo_id
            )
        except (ValueError, OSError) as exc:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def remove_repo(
        self, repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        from .....core.logging_utils import safe_log

        payload = payload or HubRemoveRepoRequest()
        force = payload.force
        force_attestation = payload.force_attestation
        delete_dir = payload.delete_dir
        delete_worktrees = payload.delete_worktrees
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub remove repo id=%s force=%s delete_dir=%s delete_worktrees=%s force_attestation=%s"
            % (repo_id, force, delete_dir, delete_worktrees, bool(force_attestation)),
        )
        remove_kwargs: dict[str, Any] = {
            "force": force,
            "delete_dir": delete_dir,
            "delete_worktrees": delete_worktrees,
        }
        if force_attestation is not None:
            remove_kwargs["force_attestation"] = _build_force_attestation_payload(
                force_attestation,
                target_scope=f"hub.remove_repo:{repo_id}",
            )
        try:
            await asyncio.to_thread(
                self._context.supervisor.remove_repo,
                repo_id,
                **remove_kwargs,
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snapshots = await asyncio.to_thread(
            self._context.supervisor.list_repos, use_cache=False
        )
        await self._mount_manager.refresh_mounts(snapshots)
        return {"status": "ok"}

    async def remove_repo_job(
        self, repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ) -> dict[str, Any]:
        from .....core.request_context import get_request_id

        payload = payload or HubRemoveRepoRequest()
        remove_kwargs: dict[str, Any] = {
            "force": payload.force,
            "delete_dir": payload.delete_dir,
            "delete_worktrees": payload.delete_worktrees,
        }
        if payload.force_attestation is not None:
            remove_kwargs["force_attestation"] = _build_force_attestation_payload(
                payload.force_attestation,
                target_scope=f"hub.remove_repo:{repo_id}",
            )

        async def _run_remove_repo():
            await asyncio.to_thread(
                self._context.supervisor.remove_repo,
                repo_id,
                **remove_kwargs,
            )
            snapshots = await asyncio.to_thread(
                self._context.supervisor.list_repos, use_cache=False
            )
            await self._mount_manager.refresh_mounts(snapshots)
            return {"status": "ok"}

        job = await self._context.job_manager.submit(
            "hub.remove_repo", _run_remove_repo, request_id=get_request_id()
        )
        return job.to_dict()


def build_hub_repo_crud_router(
    context: HubAppContext,
    mount_manager: HubMountManager,
    enricher: HubRepoEnricher,
) -> APIRouter:
    router = APIRouter()
    crud_service = HubRepoCrudService(context, mount_manager, enricher)

    @router.post("/hub/repos")
    async def create_repo(payload: HubCreateRepoRequest):
        return await crud_service.create_repo(payload)

    @router.post("/hub/jobs/repos", response_model=HubJobResponse)
    async def create_repo_job(payload: HubCreateRepoRequest):
        return await crud_service.create_repo_job(payload)

    @router.post("/hub/repos/{repo_id}/worktree-setup")
    async def set_worktree_setup(repo_id: str, payload: Annotated[Any, Body()] = None):
        return await crud_service.set_worktree_setup(repo_id, payload)

    @router.post("/hub/repos/{repo_id}/pin")
    async def pin_parent_repo(
        repo_id: str, payload: Optional[HubPinRepoRequest] = None
    ):
        return await crud_service.pin_parent_repo(repo_id, payload)

    @router.get("/hub/repos/{repo_id}/remove-check")
    async def remove_repo_check(repo_id: str):
        return await crud_service.remove_repo_check(repo_id)

    @router.post("/hub/repos/{repo_id}/remove")
    async def remove_repo(repo_id: str, payload: Optional[HubRemoveRepoRequest] = None):
        return await crud_service.remove_repo(repo_id, payload)

    @router.post("/hub/jobs/repos/{repo_id}/remove", response_model=HubJobResponse)
    async def remove_repo_job(
        repo_id: str, payload: Optional[HubRemoveRepoRequest] = None
    ):
        return await crud_service.remove_repo_job(repo_id, payload)

    return router
