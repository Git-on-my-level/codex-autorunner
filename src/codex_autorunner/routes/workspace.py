from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..core import drafts as draft_utils
from ..tickets.spec_ingest import (
    SpecIngestTicketsError,
    ingest_workspace_spec_to_tickets,
)
from ..web.schemas import (
    SpecIngestTicketsResponse,
    WorkspaceFileListResponse,
    WorkspaceResponse,
    WorkspaceWriteRequest,
)
from ..workspace.paths import (
    WORKSPACE_DOC_KINDS,
    list_workspace_files,
    normalize_workspace_rel_path,
    read_workspace_doc,
    read_workspace_file,
    workspace_doc_path,
    write_workspace_doc,
    write_workspace_file,
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

    @router.get("/workspace/files", response_model=WorkspaceFileListResponse)
    def list_files(request: Request):
        repo_root = request.app.state.engine.repo_root
        files = [asdict(item) for item in list_workspace_files(repo_root)]
        return {"files": files}

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
