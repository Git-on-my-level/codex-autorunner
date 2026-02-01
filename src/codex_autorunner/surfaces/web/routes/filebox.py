from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ....core.filebox import (
    BOXES,
    FileBoxEntry,
    delete_file,
    ensure_structure,
    list_filebox,
    migrate_legacy,
    resolve_file,
    save_file,
)
from ....core.hub import HubSupervisor
from ....core.utils import find_repo_root

logger = logging.getLogger(__name__)


def _serialize_entry(entry: FileBoxEntry, *, request: Request) -> dict[str, Any]:
    base = request.scope.get("root_path", "") or ""
    # Provide a download URL that survives base_path rewrites.
    download = f"{base}/api/filebox/{entry.box}/{entry.name}"
    return {
        "name": entry.name,
        "box": entry.box,
        "size": entry.size,
        "modified_at": entry.modified_at,
        "source": entry.source,
        "url": download,
    }


def _serialize_listing(
    entries: dict[str, list[FileBoxEntry]], *, request: Request
) -> dict[str, Any]:
    return {
        box: [_serialize_entry(e, request=request) for e in files]
        for box, files in entries.items()
    }


def _resolve_repo_root(request: Request) -> Path:
    engine = getattr(request.app.state, "engine", None)
    repo_root = getattr(engine, "repo_root", None)
    if isinstance(repo_root, Path):
        return repo_root
    if isinstance(repo_root, str):
        try:
            return Path(repo_root)
        except Exception:
            pass
    return find_repo_root()


def build_filebox_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["filebox"])

    @router.get("/filebox")
    def list_box(request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        ensure_structure(repo_root)
        try:
            migrate_legacy(repo_root)
        except Exception:
            logger.debug("FileBox legacy migration skipped", exc_info=True)
        entries = list_filebox(repo_root)
        return _serialize_listing(entries, request=request)

    @router.get("/filebox/{box}")
    def list_single_box(box: str, request: Request) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        ensure_structure(repo_root)
        entries = list_filebox(repo_root)
        return {box: _serialize_listing(entries, request=request).get(box, [])}

    @router.post("/filebox/{box}")
    async def upload_file(box: str, request: Request) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        ensure_structure(repo_root)

        form = await request.form()
        saved = []
        for filename, file in form.items():
            try:
                data = await file.read()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to read upload: %s", exc)
                continue
            try:
                path = save_file(repo_root, box, filename, data)
                saved.append(path.name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "saved": saved}

    @router.get("/filebox/{box}/{filename}")
    def download_file(box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        entry = resolve_file(repo_root, box, filename)
        if entry is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(entry.path, filename=entry.name)

    @router.delete("/filebox/{box}/{filename}")
    def delete_file_entry(box: str, filename: str, request: Request) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        removed = delete_file(repo_root, box, filename)
        if not removed:
            raise HTTPException(status_code=404, detail="File not found")
        return {"status": "ok"}

    return router


def _resolve_hub_repo_root(request: Request, repo_id: Optional[str]) -> Path:
    supervisor: HubSupervisor | None = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=404, detail="Hub supervisor unavailable")
    snapshots = supervisor.list_repos()
    candidates = [
        snap for snap in snapshots if snap.initialized and snap.exists_on_disk
    ]
    target = None
    if repo_id:
        target = next((snap for snap in candidates if snap.id == repo_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Repo not found")
    else:
        if len(candidates) == 1:
            target = candidates[0]
    if target is None:
        raise HTTPException(status_code=400, detail="repo_id is required")
    return target.path


def _serialize_hub_entry(
    entry: FileBoxEntry, *, request: Request, repo_id: str
) -> dict[str, Any]:
    base = request.scope.get("root_path", "") or ""
    download = f"{base}/hub/filebox/{repo_id}/{entry.box}/{entry.name}"
    return {
        "name": entry.name,
        "box": entry.box,
        "size": entry.size,
        "modified_at": entry.modified_at,
        "source": entry.source,
        "url": download,
        "repo_id": repo_id,
    }


def build_hub_filebox_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/filebox", tags=["filebox"])

    @router.get("/{repo_id}")
    def list_repo_filebox(repo_id: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            migrate_legacy(repo_root)
        except Exception:
            logger.debug("Hub FileBox legacy migration skipped", exc_info=True)
        entries = list_filebox(repo_root)
        serialized = {
            box: [
                _serialize_hub_entry(e, request=request, repo_id=repo_id) for e in files
            ]
            for box, files in entries.items()
        }
        return serialized

    @router.post("/{repo_id}/{box}")
    async def hub_upload(repo_id: str, box: str, request: Request) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_hub_repo_root(request, repo_id)
        ensure_structure(repo_root)
        form = await request.form()
        saved = []
        for filename, file in form.items():
            data = await file.read()
            path = save_file(repo_root, box, filename, data)
            saved.append(path.name)
        return {"status": "ok", "saved": saved}

    @router.get("/{repo_id}/{box}/{filename}")
    def hub_download(repo_id: str, box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_hub_repo_root(request, repo_id)
        entry = resolve_file(repo_root, box, filename)
        if entry is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(entry.path, filename=entry.name)

    @router.delete("/{repo_id}/{box}/{filename}")
    def hub_delete(
        repo_id: str, box: str, filename: str, request: Request
    ) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_hub_repo_root(request, repo_id)
        removed = delete_file(repo_root, box, filename)
        if not removed:
            raise HTTPException(status_code=404, detail="File not found")
        return {"status": "ok"}

    return router


__all__ = ["build_filebox_routes", "build_hub_filebox_routes"]
