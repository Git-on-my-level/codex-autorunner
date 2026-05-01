from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from ....core.filebox import (
    BOXES,
    FileBoxEntry,
    ensure_structure,
    list_filebox,
    open_file,
    sanitize_filename,
    save_file,
)
from ....core.filebox import (
    delete_file as delete_filebox_file,
)
from ....core.hub import HubSupervisor
from ....core.utils import find_repo_root

logger = logging.getLogger(__name__)
DEFAULT_FILEBOX_MAX_UPLOAD_BYTES = 10_000_000
UPLOAD_CHUNK_BYTES = 1024 * 1024


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
    ensure_structure(repo_root)
    form = await request.form()
    max_upload_bytes = _resolve_max_upload_bytes(request)
    pending: list[tuple[str, bytes]] = []
    for filename, file in form.items():
        if not isinstance(file, UploadFile):
            continue
        try:
            sanitize_filename(filename)
            effective_filename = sanitize_filename(file.filename or filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            data = await _read_upload_limited(file, max_upload_bytes=max_upload_bytes)
        except OSError as exc:
            logger.warning("Failed to read upload: %s", exc)
            continue
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        pending.append((effective_filename, data))

    saved = []
    for filename, data in pending:
        try:
            path = save_file(repo_root, box, filename, data)
            saved.append(path.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "saved": saved}


def _resolve_max_upload_bytes(request: Request) -> int:
    config = getattr(request.app.state, "config", None)
    pma_config = getattr(config, "pma", None)
    raw = getattr(pma_config, "max_upload_bytes", None)
    if raw is None:
        return DEFAULT_FILEBOX_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_FILEBOX_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_FILEBOX_MAX_UPLOAD_BYTES


async def _read_upload_limited(file: UploadFile, *, max_upload_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_upload_bytes:
            raise ValueError(f"File too large (max {max_upload_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def build_filebox_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["filebox"])

    @router.get("/filebox")
    def list_box(request: Request) -> dict[str, Any]:
        repo_root = _resolve_repo_root(request)
        ensure_structure(repo_root)
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
        return await _upload_files_to_box(repo_root=repo_root, box=box, request=request)

    @router.get("/filebox/{box}/{filename}")
    def download_file(box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        result = open_file(repo_root, box, filename)
        if result is None:
            raise HTTPException(status_code=404, detail="File not found")
        entry, handle = result
        return _file_download_response(entry, handle)

    @router.delete("/filebox/{box}/{filename}")
    def delete_file_entry(box: str, filename: str, request: Request) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_repo_root(request)
        try:
            removed = delete_filebox_file(repo_root, box, filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail="Failed to delete file"
            ) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="File not found") from None
        return {"status": "ok"}

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
        return await _upload_files_to_box(repo_root=repo_root, box=box, request=request)

    @router.get("/{repo_id}/{box}/{filename}")
    def hub_download(repo_id: str, box: str, filename: str, request: Request):
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_hub_repo_root(request, repo_id)
        result = open_file(repo_root, box, filename)
        if result is None:
            raise HTTPException(status_code=404, detail="File not found")
        entry, handle = result
        return _file_download_response(entry, handle)

    @router.delete("/{repo_id}/{box}/{filename}")
    def hub_delete(
        repo_id: str, box: str, filename: str, request: Request
    ) -> dict[str, Any]:
        if box not in BOXES:
            raise HTTPException(status_code=400, detail="Invalid box")
        repo_root = _resolve_hub_repo_root(request, repo_id)
        try:
            removed = delete_filebox_file(repo_root, box, filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail="Failed to delete file"
            ) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="File not found") from None
        return {"status": "ok"}

    return router


__all__ = ["build_filebox_routes", "build_hub_filebox_routes"]
