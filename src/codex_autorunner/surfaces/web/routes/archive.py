"""Archive browsing routes for repo-mode servers."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..schemas import (
    ArchiveSnapshotDetailResponse,
    ArchiveSnapshotsResponse,
    ArchiveTreeResponse,
    LocalRunArchivesResponse,
)
from .archive_helpers import (
    iter_local_run_archives,
    iter_snapshots,
    list_tree,
    load_meta,
    normalize_archive_rel_path,
    resolve_local_run_root,
    resolve_snapshot_root,
    snapshot_summary,
)


def build_archive_routes() -> APIRouter:
    router = APIRouter(prefix="/api/archive", tags=["archive"])

    @router.get("/snapshots", response_model=ArchiveSnapshotsResponse)
    def list_snapshots(request: Request):
        repo_root = request.app.state.engine.repo_root
        snapshots = iter_snapshots(repo_root)
        return {"snapshots": snapshots}

    @router.get("/local-runs", response_model=LocalRunArchivesResponse)
    def list_local_runs(request: Request):
        repo_root = request.app.state.engine.repo_root
        archives = iter_local_run_archives(repo_root)
        return {"archives": archives}

    @router.get(
        "/snapshots/{snapshot_id}", response_model=ArchiveSnapshotDetailResponse
    )
    def get_snapshot(
        request: Request, snapshot_id: str, worktree_repo_id: Optional[str] = None
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, worktree_id = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        meta = load_meta(snapshot_root / "META.json")
        summary = snapshot_summary(snapshot_root, worktree_id, meta)
        return {"snapshot": summary, "meta": meta}

    @router.get("/tree", response_model=ArchiveTreeResponse)
    def list_tree_endpoint(
        request: Request,
        snapshot_id: str,
        path: str = "",
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            response = list_tree(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return response

    @router.get("/local/tree", response_model=ArchiveTreeResponse)
    def list_local_tree(
        request: Request,
        run_id: str,
        path: str = "",
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = resolve_local_run_root(repo_root, run_id)
            response = list_tree(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return response

    @router.get("/file", response_class=PlainTextResponse)
    def read_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            target, _ = normalize_archive_rel_path(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # codeql[py/path-injection]
        if not target.exists():
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        try:
            # codeql[py/path-injection]
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.get("/local/file", response_class=PlainTextResponse)
    def read_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = resolve_local_run_root(repo_root, run_id)
            target, rel_posix = normalize_archive_rel_path(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not rel_posix:
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if not target.exists():
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")

        try:
            # codeql[py/path-injection]
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.get("/download")
    def download_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            target, _ = normalize_archive_rel_path(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # codeql[py/path-injection]
        if not target.exists():
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        # codeql[py/path-injection]
        return FileResponse(
            path=target,
            filename=target.name,
        )

    @router.get("/local/download")
    def download_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = resolve_local_run_root(repo_root, run_id)
            target, rel_posix = normalize_archive_rel_path(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not rel_posix:
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if not target.exists():
            raise HTTPException(status_code=404, detail="file not found")
        # codeql[py/path-injection]
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")

        # codeql[py/path-injection]
        return FileResponse(
            path=target,
            filename=target.name,
        )

    return router


__all__ = ["build_archive_routes"]
