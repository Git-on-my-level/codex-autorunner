from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator, NoReturn, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ....core.filebox import FileBoxEntry
from ....core.hub import HubSupervisor
from ....core.utils import find_repo_root
from ..services.workspace_resources import (
    FileBoxResourceService,
    FileBoxUrlScope,
    WorkspaceResourceError,
    read_upload_limited,
    resolve_max_upload_bytes,
)

logger = logging.getLogger(__name__)


def _raise_http(exc: WorkspaceResourceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _resolve_repo_root(request: Request) -> Path:
    engine = getattr(request.app.state, "engine", None)
    repo_root = getattr(engine, "repo_root", None)
    if isinstance(repo_root, Path):
        return repo_root
    if isinstance(repo_root, str):
        try:
            return Path(repo_root)
        except (TypeError, ValueError):
            logger.debug("Failed to convert repo_root string to Path", exc_info=True)
    return find_repo_root()


def _stream_file(handle: BinaryIO) -> Iterator[bytes]:
    try:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        handle.close()


def _file_download_response(entry: FileBoxEntry, handle: BinaryIO) -> StreamingResponse:
    encoded = quote(entry.name, safe="")
    content_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    if entry.size is not None:
        headers["Content-Length"] = str(entry.size)
    if entry.modified_at:
        try:
            modified = datetime.fromisoformat(entry.modified_at)
            headers["Last-Modified"] = format_datetime(modified, usegmt=True)
            headers["ETag"] = f'W/"{entry.size or 0}-{int(modified.timestamp())}"'
        except ValueError:
            pass
    return StreamingResponse(
        _stream_file(handle),
        media_type=content_type,
        headers=headers,
    )


async def _upload_files_to_box(
    *, repo_root: Path, box: str, request: Request
) -> dict[str, Any]:
    form = await request.form()
    try:
        return await FileBoxResourceService().upload_files_to_box(
            repo_root=repo_root,
            box=box,
            form=form,
            max_upload_bytes=_resolve_max_upload_bytes(request),
        )
    except WorkspaceResourceError as exc:
        _raise_http(exc)


def _resolve_max_upload_bytes(request: Request) -> int:
    config = getattr(request.app.state, "config", None)
    pma_config = getattr(config, "pma", None)
    return resolve_max_upload_bytes(getattr(pma_config, "max_upload_bytes", None))


_read_upload_limited = read_upload_limited


def _filebox_url_scope(request: Request, repo_id: str | None = None) -> FileBoxUrlScope:
    return FileBoxUrlScope(
        root_path=request.scope.get("root_path", "") or "", repo_id=repo_id
    )


def build_filebox_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["filebox"])
    service = FileBoxResourceService()

    @router.get("/filebox")
    def list_box(request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        try:
            return service.list_filebox(
                repo_root, url_scope=_filebox_url_scope(request)
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.get("/artifacts/deliveries")
    def list_artifact_deliveries(
        request: Request,
        state: Optional[str] = None,
        surface: Optional[str] = None,
        conversation: Optional[str] = None,
    ) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        return service.list_artifact_deliveries(
            repo_root,
            url_scope=_filebox_url_scope(request),
            state=state,
            surface=surface,
            conversation=conversation,
        )

    @router.get("/artifacts/deliveries/{delivery_id}/download")
    def download_artifact_delivery(delivery_id: str, request: Request):
        repo_root = _resolve_repo_root(request)
        try:
            resource = service.open_delivery_artifact(repo_root, delivery_id)
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return _file_download_response(resource.entry, resource.handle)

    @router.get("/filebox/{box}")
    def list_single_box(box: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        try:
            return service.list_single_box(
                repo_root, box, url_scope=_filebox_url_scope(request)
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.post("/filebox/{box}")
    async def upload_file(box: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        return await _upload_files_to_box(repo_root=repo_root, box=box, request=request)

    @router.get("/filebox/{box}/{filename}")
    def download_file(box: str, filename: str, request: Request):
        repo_root = _resolve_repo_root(request)
        try:
            resource = service.open_download(repo_root, box, filename)
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return _file_download_response(resource.entry, resource.handle)

    @router.delete("/filebox/{box}/{filename}")
    def delete_file_entry(box: str, filename: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        try:
            return service.delete_file(repo_root, box, filename)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    return router


def _resolve_hub_repo_root(request: Request, repo_id: Optional[str]) -> Path:
    supervisor: HubSupervisor | None = getattr(
        request.app.state, "hub_supervisor", None
    )
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


def build_hub_filebox_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/filebox", tags=["filebox"])
    service = FileBoxResourceService()

    @router.get("/{repo_id}")
    def list_repo_filebox(repo_id: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            return service.list_filebox(
                repo_root, url_scope=_filebox_url_scope(request, repo_id)
            )
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.get("/{repo_id}/artifacts/deliveries")
    def list_repo_artifact_deliveries(
        repo_id: str,
        request: Request,
        state: Optional[str] = None,
        surface: Optional[str] = None,
        conversation: Optional[str] = None,
    ) -> dict[str, Any]:
        repo_root = _resolve_hub_repo_root(request, repo_id)
        return service.list_artifact_deliveries(
            repo_root,
            url_scope=_filebox_url_scope(request, repo_id),
            state=state,
            surface=surface,
            conversation=conversation,
            repo_id=repo_id,
        )

    @router.get("/{repo_id}/artifacts/deliveries/{delivery_id}/download")
    def download_repo_artifact_delivery(
        repo_id: str, delivery_id: str, request: Request
    ):
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            resource = service.open_delivery_artifact(repo_root, delivery_id)
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return _file_download_response(resource.entry, resource.handle)

    @router.post("/{repo_id}/{box}")
    async def hub_upload(repo_id: str, box: str, request: Request) -> dict[str, Any]:
        repo_root = _resolve_hub_repo_root(request, repo_id)
        return await _upload_files_to_box(repo_root=repo_root, box=box, request=request)

    @router.get("/{repo_id}/{box}/{filename}")
    def hub_download(repo_id: str, box: str, filename: str, request: Request):
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            resource = service.open_download(repo_root, box, filename)
        except WorkspaceResourceError as exc:
            _raise_http(exc)
        return _file_download_response(resource.entry, resource.handle)

    @router.delete("/{repo_id}/{box}/{filename}")
    def hub_delete(
        repo_id: str, box: str, filename: str, request: Request
    ) -> dict[str, Any]:
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            return service.delete_file(repo_root, box, filename)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    return router


__all__ = ["build_filebox_routes", "build_hub_filebox_routes"]
