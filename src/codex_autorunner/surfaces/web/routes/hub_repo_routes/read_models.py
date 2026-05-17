from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from fastapi import APIRouter, HTTPException

from ...services.repo_worktree_read_models import RepoWorktreeReadModelService

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


def build_hub_repo_read_model_router(
    context: HubAppContext,
    mount_manager: HubMountManager,
    enricher: HubRepoEnricher,
) -> APIRouter:
    router = APIRouter()
    service = RepoWorktreeReadModelService(context, mount_manager, enricher)

    @router.get("/hub/read-models/repo-worktree/topology")
    async def repo_worktree_topology(
        kind: str = "all", limit: int = 50, cursor: Optional[str] = None
    ):
        if kind not in {"all", "repo", "worktree"}:
            raise HTTPException(
                status_code=400, detail="kind must be all, repo, or worktree"
            )
        return await service.topology(kind=kind, limit=limit, cursor=cursor)

    @router.get("/hub/read-models/repo-worktree/runtime")
    async def repo_worktree_runtime(
        kind: str = "all", limit: int = 50, cursor: Optional[str] = None
    ):
        if kind not in {"all", "repo", "worktree"}:
            raise HTTPException(
                status_code=400, detail="kind must be all, repo, or worktree"
            )
        return await service.runtime(kind=kind, limit=limit, cursor=cursor)

    @router.get("/hub/read-models/repos/{repo_id}/detail")
    async def repo_detail(
        repo_id: str,
        ticket_limit: int = 25,
        run_limit: int = 10,
        chat_limit: int = 25,
        artifact_limit: int = 25,
    ):
        return await service.detail(
            owner_kind="repo",
            owner_id=repo_id,
            ticket_limit=ticket_limit,
            run_limit=run_limit,
            chat_limit=chat_limit,
            artifact_limit=artifact_limit,
        )

    @router.get("/hub/read-models/worktrees/{worktree_id}/detail")
    async def worktree_detail(
        worktree_id: str,
        ticket_limit: int = 25,
        run_limit: int = 10,
        chat_limit: int = 25,
        artifact_limit: int = 25,
    ):
        return await service.detail(
            owner_kind="worktree",
            owner_id=worktree_id,
            ticket_limit=ticket_limit,
            run_limit=run_limit,
            chat_limit=chat_limit,
            artifact_limit=artifact_limit,
        )

    @router.get("/hub/read-models/tickets/{ticket_id}")
    async def ticket_detail(
        ticket_id: str, owner_kind: Literal["repo", "worktree"], owner_id: str
    ):
        return await service.ticket_detail(
            owner_kind=owner_kind,
            owner_id=owner_id,
            ticket_id=ticket_id,
        )

    return router


__all__ = ["build_hub_repo_read_model_router"]
