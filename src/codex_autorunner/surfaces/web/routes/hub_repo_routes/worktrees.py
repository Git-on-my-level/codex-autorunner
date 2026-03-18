from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import HTTPException

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from ...schemas import (
        HubCleanupWorktreeRequest,
        HubCreateWorktreeRequest,
    )
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


class HubWorktreeService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
        build_force_attestation_payload: callable,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._enricher = enricher
        self._build_force_attestation_payload = build_force_attestation_payload

    async def create_worktree(
        self,
        base_repo_id: str,
        branch: str,
        force: bool,
        start_point: Optional[str],
    ) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub create worktree base=%s branch=%s force=%s start_point=%s"
            % (base_repo_id, branch, force, start_point),
        )
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.create_worktree,
                base_repo_id=str(base_repo_id),
                branch=str(branch),
                force=force,
                start_point=str(start_point) if start_point else None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return self._enricher.enrich_repo(snapshot)

    async def create_worktree_job(self, payload: HubCreateWorktreeRequest) -> dict:
        async def _run_create_worktree():
            snapshot = await asyncio.to_thread(
                self._context.supervisor.create_worktree,
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
                start_point=str(payload.start_point) if payload.start_point else None,
            )
            await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return self._enricher.enrich_repo(snapshot)

        job = await self._context.job_manager.submit(
            "hub.create_worktree",
            _run_create_worktree,
            request_id=self._get_request_id(),
        )
        return job.to_dict()

    def _get_request_id(self) -> Optional[str]:
        from .....core.request_context import get_request_id

        return get_request_id()

    async def cleanup_worktree(
        self,
        worktree_repo_id: str,
        delete_branch: bool,
        delete_remote: bool,
        archive: bool,
        force: bool,
        force_attestation: Optional[str],
        force_archive: bool,
        archive_note: Optional[str],
        archive_profile: Optional[str],
    ) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub cleanup worktree id=%s delete_branch=%s delete_remote=%s archive=%s force=%s force_archive=%s force_attestation=%s"
            % (
                worktree_repo_id,
                delete_branch,
                delete_remote,
                archive,
                force,
                force_archive,
                bool(force_attestation),
            ),
        )
        cleanup_kwargs: dict[str, Any] = {
            "worktree_repo_id": str(worktree_repo_id),
            "delete_branch": delete_branch,
            "delete_remote": delete_remote,
            "archive": archive,
            "force": force,
            "force_archive": force_archive,
            "archive_note": archive_note,
            "archive_profile": archive_profile,
        }
        if force_attestation is not None:
            cleanup_kwargs["force_attestation"] = self._build_force_attestation_payload(
                force_attestation,
                target_scope=f"hub.worktree.cleanup:{worktree_repo_id}",
            )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.cleanup_worktree,
                **cleanup_kwargs,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(result, dict):
            return result
        return {"status": "ok"}

    async def cleanup_worktree_job(self, payload: HubCleanupWorktreeRequest) -> dict:
        cleanup_kwargs: dict[str, Any] = {
            "worktree_repo_id": str(payload.worktree_repo_id),
            "delete_branch": payload.delete_branch,
            "delete_remote": payload.delete_remote,
            "archive": payload.archive,
            "force": payload.force,
            "force_archive": payload.force_archive,
            "archive_note": payload.archive_note,
            "archive_profile": payload.archive_profile,
        }
        if payload.force_attestation is not None:
            cleanup_kwargs["force_attestation"] = self._build_force_attestation_payload(
                payload.force_attestation,
                target_scope=f"hub.worktree.cleanup:{payload.worktree_repo_id}",
            )

        def _run_cleanup_worktree():
            result = self._context.supervisor.cleanup_worktree(**cleanup_kwargs)
            if isinstance(result, dict):
                return result
            return {"status": "ok"}

        job = await self._context.job_manager.submit(
            "hub.cleanup_worktree",
            _run_cleanup_worktree,
            request_id=self._get_request_id(),
        )
        return job.to_dict()

    async def archive_worktree(
        self,
        worktree_repo_id: str,
        archive_note: Optional[str],
        archive_profile: Optional[str],
    ) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub archive worktree id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.archive_worktree,
                worktree_repo_id=str(worktree_repo_id),
                archive_note=archive_note,
                archive_profile=archive_profile,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    async def archive_worktree_state(
        self,
        worktree_repo_id: str,
        archive_note: Optional[str],
        archive_profile: Optional[str],
    ) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub archive worktree state id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.archive_repo_state,
                repo_id=str(worktree_repo_id),
                archive_note=archive_note,
                archive_profile=archive_profile,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result
