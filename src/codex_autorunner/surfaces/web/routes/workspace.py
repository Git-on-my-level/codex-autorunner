from __future__ import annotations

import io
import zipfile
from dataclasses import asdict

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from ...core import drafts as draft_utils
from ...tickets.spec_ingest import (
    SpecIngestTicketsError,
    ingest_workspace_spec_to_tickets,
)
from ...workspace.paths import (
    PINNED_DOC_FILENAMES,
    WORKSPACE_DOC_KINDS,
    list_workspace_files,
    list_workspace_tree,
    normalize_workspace_rel_path,
    read_workspace_doc,
    read_workspace_file,
    sanitize_workspace_filename,
    workspace_dir,
    workspace_doc_path,
    write_workspace_doc,
    write_workspace_file,
)
from .schemas import (
    SpecIngestTicketsResponse,
    WorkspaceFileListResponse,
    WorkspaceResponse,
    WorkspaceTreeResponse,
    WorkspaceUploadResponse,
    WorkspaceWriteRequest,
)


def build_workspace_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["workspace"])

    @router.get("/workspace", response_model=WorkspaceResponse)
    def get_workspace(request: Request):
        repo_root = request.app.state.engine.repo_root
        return {
            "active_context": read_workspace_doc(repo_root, "active_context"),
            "decisions": read_workspace_doc(repo_root, "decisions"),
            "spec": read_workspace_doc(repo_root, "spec"),
        }

    @router.get("/workspace/file", response_class=PlainTextResponse)
    def read_workspace(request: Request, path: str):
        repo_root = request.app.state.engine.repo_root
        try:
            content = read_workspace_file(repo_root, path)
        except ValueError as exc:  # invalid path
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.put("/workspace/file", response_class=PlainTextResponse)
    def write_workspace(request: Request, payload: WorkspaceWriteRequest, path: str):
        repo_root = request.app.state.engine.repo_root
        try:
            # Normalize path the same way workspace helpers do to avoid traversal
            safe_path, rel_posix = normalize_workspace_rel_path(repo_root, path)
            content = write_workspace_file(repo_root, path, payload.content)
            try:
                rel_repo_path = safe_path.relative_to(repo_root).as_posix()
                draft_utils.invalidate_drafts_for_path(repo_root, rel_repo_path)
                state_key = f"workspace_{rel_posix.replace('/', '_')}"
                draft_utils.remove_draft(repo_root, state_key)
            except Exception:
                pass
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.put("/workspace/{kind}", response_model=WorkspaceResponse)
    def put_workspace(kind: str, payload: WorkspaceWriteRequest, request: Request):
        key = (kind or "").strip().lower()
        if key not in WORKSPACE_DOC_KINDS:
            raise HTTPException(status_code=400, detail="invalid workspace doc kind")
        repo_root = request.app.state.engine.repo_root
        write_workspace_doc(repo_root, key, payload.content)
        try:
            rel_path = workspace_doc_path(repo_root, key).relative_to(repo_root)
            draft_utils.invalidate_drafts_for_path(repo_root, rel_path.as_posix())
            state_key = f"workspace_{rel_path.name}"
            draft_utils.remove_draft(repo_root, state_key)
        except Exception:
            # best-effort invalidation; avoid blocking writes
            pass
        return {
            "active_context": read_workspace_doc(repo_root, "active_context"),
            "decisions": read_workspace_doc(repo_root, "decisions"),
            "spec": read_workspace_doc(repo_root, "spec"),
        }

    @router.get("/workspace/files", response_model=WorkspaceFileListResponse)
    def list_files(request: Request):
        repo_root = request.app.state.engine.repo_root
        files = [asdict(item) for item in list_workspace_files(repo_root)]
        return {"files": files}

    @router.get("/workspace/tree", response_model=WorkspaceTreeResponse)
    def get_workspace_tree(request: Request):
        repo_root = request.app.state.engine.repo_root
        tree = [asdict(item) for item in list_workspace_tree(repo_root)]
        return {"tree": tree}

    @router.post("/workspace/upload", response_model=WorkspaceUploadResponse)
    async def upload_workspace_files(
        request: Request,
        files: list[UploadFile] = File(...),  # noqa: B008
        subdir: str = Form(""),
    ):
        if not files:
            raise HTTPException(status_code=400, detail="no files provided")

        repo_root = request.app.state.engine.repo_root
        base = workspace_dir(repo_root)
        target_dir = base
        if subdir:
            try:
                target_dir, _ = normalize_workspace_rel_path(repo_root, subdir)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        target_dir.mkdir(parents=True, exist_ok=True)

        uploaded: list[dict[str, str | int]] = []
        for upload in files:
            filename = sanitize_workspace_filename(upload.filename or "")
            try:
                data = await upload.read()
            except (
                Exception
            ) as exc:  # pragma: no cover - handled by FastAPI for most cases
                raise HTTPException(
                    status_code=400, detail="failed to read upload"
                ) from exc

            dest = target_dir / filename
            dest.write_bytes(
                data
            )  # codeql[py/path-injection] dest sits under normalized workspace dir
            rel_path = dest.relative_to(base).as_posix()
            uploaded.append({"filename": filename, "path": rel_path, "size": len(data)})

        return {"status": "ok", "uploaded": uploaded}

    @router.get("/workspace/download")
    async def download_workspace_file(request: Request, path: str):
        repo_root = request.app.state.engine.repo_root
        try:
            safe_path, _ = normalize_workspace_rel_path(repo_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not safe_path.exists() or safe_path.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        return FileResponse(
            path=safe_path, filename=safe_path.name
        )  # codeql[py/path-injection] safe_path validated by normalize_workspace_rel_path

    @router.get("/workspace/download-zip")
    async def download_workspace_zip(request: Request, path: str = ""):
        repo_root = request.app.state.engine.repo_root
        base = workspace_dir(repo_root)
        base.mkdir(parents=True, exist_ok=True)

        target_dir = base
        zip_name = "workspace.zip"
        if path:
            try:
                target_dir, _ = normalize_workspace_rel_path(repo_root, path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not target_dir.exists() or not target_dir.is_dir():
                raise HTTPException(status_code=404, detail="folder not found")
            zip_name = f"{target_dir.name}.zip"

        buffer = io.BytesIO()
        base_real = base.resolve()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in target_dir.rglob("*"):
                if file_path.is_dir():
                    continue
                if file_path.is_symlink():
                    try:
                        file_path.resolve().relative_to(base_real)
                    except Exception:
                        continue
                arc_name = file_path.relative_to(target_dir).as_posix()
                zf.write(
                    file_path, arc_name
                )  # codeql[py/path-injection] file_path constrained to workspace dir

        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )

    @router.post("/workspace/folder")
    async def create_workspace_folder(request: Request, path: str):
        repo_root = request.app.state.engine.repo_root
        try:
            safe_path, rel_posix = normalize_workspace_rel_path(repo_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if safe_path.exists():
            raise HTTPException(status_code=400, detail="path already exists")

        safe_path.mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": rel_posix}

    @router.delete("/workspace/folder")
    async def delete_workspace_folder(request: Request, path: str):
        repo_root = request.app.state.engine.repo_root
        try:
            safe_path, rel_posix = normalize_workspace_rel_path(repo_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not safe_path.exists():
            raise HTTPException(status_code=404, detail="folder not found")
        if not safe_path.is_dir():
            raise HTTPException(status_code=400, detail="not a folder")
        if any(safe_path.iterdir()):
            raise HTTPException(status_code=400, detail="folder not empty")

        safe_path.rmdir()
        return {"status": "deleted", "path": rel_posix}

    @router.delete("/workspace/file")
    async def delete_workspace_file(request: Request, path: str):
        repo_root = request.app.state.engine.repo_root
        base = workspace_dir(repo_root)
        try:
            safe_path, rel_posix = normalize_workspace_rel_path(repo_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if safe_path.parent == base and safe_path.name in PINNED_DOC_FILENAMES:
            raise HTTPException(status_code=400, detail="cannot delete pinned docs")
        if not safe_path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        if safe_path.is_dir():
            raise HTTPException(status_code=400, detail="use folder delete endpoint")

        safe_path.unlink()  # codeql[py/path-injection] safe_path validated by normalize_workspace_rel_path
        return {"status": "deleted", "path": rel_posix}

    @router.post("/workspace/spec/ingest", response_model=SpecIngestTicketsResponse)
    def ingest_workspace_spec(request: Request):
        repo_root = request.app.state.engine.repo_root
        try:
            result = ingest_workspace_spec_to_tickets(repo_root)
        except SpecIngestTicketsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "created": result.created,
            "first_ticket_path": result.first_ticket_path,
        }

    return router
