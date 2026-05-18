"""Archive browsing routes for repo-mode servers."""

from __future__ import annotations

from typing import NoReturn, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..schemas import (
    ArchiveSnapshotDetailResponse,
    ArchiveSnapshotsResponse,
    ArchiveTreeResponse,
    LocalRunArchivesResponse,
)
from ..services.workspace_resources import (
    ArchiveResourceService,
    WorkspaceResourceError,
)


def _raise_http(exc: WorkspaceResourceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def build_archive_routes() -> APIRouter:
    router = APIRouter(prefix="/api/archive", tags=["archive"])
    service = ArchiveResourceService()

    @router.get("/snapshots", response_model=ArchiveSnapshotsResponse)
    def list_snapshots(request: Request):
        repo_root = request.app.state.engine.repo_root
        return service.list_snapshots(repo_root)

    @router.get("/local-runs", response_model=LocalRunArchivesResponse)
    def list_local_runs(request: Request):
        repo_root = request.app.state.engine.repo_root
        return service.list_local_runs(repo_root)

    @router.get(
        "/snapshots/{snapshot_id}", response_model=ArchiveSnapshotDetailResponse
    )
    def get_snapshot(
        request: Request, snapshot_id: str, worktree_repo_id: Optional[str] = None
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            return service.snapshot_detail(repo_root, snapshot_id, worktree_repo_id)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.get("/tree", response_model=ArchiveTreeResponse)
    def list_tree_endpoint(
        request: Request,
        snapshot_id: str,
        path: str = "",
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            return service.snapshot_tree(repo_root, snapshot_id, path, worktree_repo_id)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.get("/local/tree", response_model=ArchiveTreeResponse)
    def list_local_tree(
        request: Request,
        run_id: str,
        path: str = "",
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            return service.local_run_tree(repo_root, run_id, path)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.get("/file", response_class=PlainTextResponse)
    def read_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            resource = service.snapshot_file(
                repo_root,
                snapshot_id,
                path,
                worktree_repo_id=worktree_repo_id,
                include_content=True,
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return PlainTextResponse(resource.content or "")

    @router.get("/local/file", response_class=PlainTextResponse)
    def read_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            resource = service.local_run_file(
                repo_root, run_id, path, include_content=True
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return PlainTextResponse(resource.content or "")

    @router.get("/download")
    def download_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            resource = service.snapshot_file(
                repo_root,
                snapshot_id,
                path,
                worktree_repo_id=worktree_repo_id,
                include_content=False,
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)

        return FileResponse(
            path=resource.path,
            filename=resource.filename,
        )

    @router.get("/local/download")
    def download_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            resource = service.local_run_file(
                repo_root, run_id, path, include_content=False
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)

        return FileResponse(
            path=resource.path,
            filename=resource.filename,
        )

    return router


__all__ = ["build_archive_routes"]
