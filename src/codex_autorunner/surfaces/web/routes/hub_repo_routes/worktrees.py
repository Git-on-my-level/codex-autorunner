from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from fastapi import HTTPException

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from ...schemas import (
        HubCreateWorktreeRequest,
        HubRetireWorktreeRequest,
    )
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


class HubWorktreeService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
        build_force_attestation_payload: Callable[..., Optional[dict[str, str]]],
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
    ) -> dict[str, Any]:
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
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        return cast(dict[str, Any], self._enricher.enrich_repo(snapshot))

    async def create_worktree_job(
        self, payload: HubCreateWorktreeRequest
    ) -> dict[str, Any]:
        async def _run_create_worktree():
            snapshot = await asyncio.to_thread(
                self._context.supervisor.create_worktree,
                base_repo_id=str(payload.base_repo_id),
                branch=str(payload.branch),
                force=payload.force,
                start_point=str(payload.start_point) if payload.start_point else None,
            )
            await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
            return cast(dict[str, Any], self._enricher.enrich_repo(snapshot))

        job = await self._context.job_manager.submit(
            "hub.create_worktree",
            _run_create_worktree,
            request_id=self._get_request_id(),
        )
        return job.to_dict()

    def _get_request_id(self) -> Optional[str]:
        from .....core.request_context import get_request_id

        return get_request_id()

    async def retire_worktree(
        self,
        worktree_repo_id: str,
        delete_branch: bool,
        delete_remote: bool,
        force: bool,
        force_attestation: Optional[str],
        force_retire: bool,
        retire_note: Optional[str],
        retire_profile: Optional[str],
    ) -> dict[str, Any]:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub retire worktree id=%s delete_branch=%s delete_remote=%s force=%s force_retire=%s force_attestation=%s"
            % (
                worktree_repo_id,
                delete_branch,
                delete_remote,
                force,
                force_retire,
                bool(force_attestation),
            ),
        )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.retire_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                force=force,
                force_attestation=(
                    self._build_force_attestation_payload(
                        force_attestation,
                        target_scope=f"hub.worktree.retire:{worktree_repo_id}",
                    )
                    if force_attestation is not None
                    else None
                ),
                force_archive=force_retire,
                archive_note=retire_note,
                archive_profile=retire_profile,
            )
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        self._enricher.invalidate_runtime_caches()
        if isinstance(result, dict):
            return result
        return {"status": "ok"}

    async def retire_worktree_job(
        self, payload: HubRetireWorktreeRequest
    ) -> dict[str, Any]:
        def _run_retire_worktree():
            result = self._context.supervisor.retire_worktree(
                worktree_repo_id=str(payload.worktree_repo_id),
                delete_branch=payload.delete_branch,
                delete_remote=payload.delete_remote,
                force=payload.force,
                force_attestation=(
                    self._build_force_attestation_payload(
                        payload.force_attestation,
                        target_scope=f"hub.worktree.retire:{payload.worktree_repo_id}",
                    )
                    if payload.force_attestation is not None
                    else None
                ),
                force_archive=payload.force_retire,
                archive_note=payload.retire_note,
                archive_profile=payload.retire_profile,
            )
            self._enricher.invalidate_runtime_caches()
            if isinstance(result, dict):
                return result
            return {"status": "ok"}

        job = await self._context.job_manager.submit(
            "hub.retire_worktree",
            _run_retire_worktree,
            request_id=self._get_request_id(),
        )
        return job.to_dict()

    async def delete_worktree(
        self,
        worktree_repo_id: str,
        delete_branch: bool,
        delete_remote: bool,
        force: bool,
        force_attestation: Optional[str],
    ) -> dict[str, Any]:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub delete worktree id=%s force=%s force_attestation=%s"
            % (worktree_repo_id, force, bool(force_attestation)),
        )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.delete_worktree,
                worktree_repo_id=str(worktree_repo_id),
                delete_branch=delete_branch,
                delete_remote=delete_remote,
                force=force,
                force_attestation=(
                    self._build_force_attestation_payload(
                        force_attestation,
                        target_scope=f"hub.worktree.delete:{worktree_repo_id}",
                    )
                    if force_attestation is not None
                    else None
                ),
            )
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        self._enricher.invalidate_runtime_caches()
        return cast(dict[str, Any], result)

    async def retire_worktree_state(
        self,
        worktree_repo_id: str,
        retire_note: Optional[str],
        retire_profile: Optional[str],
    ) -> dict[str, Any]:
        from .....core.logging_utils import safe_log

        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub retire worktree state id=%s" % (worktree_repo_id,),
        )
        try:
            result = await asyncio.to_thread(
                self._context.supervisor.archive_repo_state,
                repo_id=str(worktree_repo_id),
                archive_note=retire_note,
                archive_profile=retire_profile,
            )
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        self._enricher.invalidate_runtime_caches()
        return cast(dict[str, Any], result)
