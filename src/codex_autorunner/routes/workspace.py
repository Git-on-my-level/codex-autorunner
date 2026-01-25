from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..tickets.spec_ingest import (
    SpecIngestTicketsError,
    ingest_workspace_spec_to_tickets,
)
from ..web.schemas import (
    SpecIngestTicketsResponse,
    WorkspaceResponse,
    WorkspaceWriteRequest,
)
from ..workspace.paths import (
    WORKSPACE_DOC_KINDS,
    read_workspace_doc,
    write_workspace_doc,
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
        return {
            "active_context": read_workspace_doc(repo_root, "active_context"),
            "decisions": read_workspace_doc(repo_root, "decisions"),
            "spec": read_workspace_doc(repo_root, "spec"),
        }

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
