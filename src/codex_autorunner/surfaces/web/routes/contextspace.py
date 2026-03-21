from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ....contextspace.paths import (
    CONTEXTSPACE_DOC_KINDS,
    read_contextspace_docs,
    serialize_contextspace_doc_catalog,
    write_contextspace_doc,
)
from ....tickets.spec_ingest import (
    SpecIngestTicketsError,
    ingest_workspace_spec_to_tickets,
)
from ..schemas import (
    ContextspaceResponse,
    ContextspaceWriteRequest,
    SpecIngestTicketsResponse,
)


def build_contextspace_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["contextspace"])

    def _contextspace_payload(repo_root):
        docs = read_contextspace_docs(repo_root)
        return {
            "active_context": docs["active_context"],
            "decisions": docs["decisions"],
            "spec": docs["spec"],
            "kinds": serialize_contextspace_doc_catalog(),
        }

    @router.get("/contextspace", response_model=ContextspaceResponse)
    def get_contextspace(request: Request):
        repo_root = request.app.state.engine.repo_root
        return _contextspace_payload(repo_root)

    @router.put("/contextspace/{kind}", response_model=ContextspaceResponse)
    def put_contextspace(
        kind: str, payload: ContextspaceWriteRequest, request: Request
    ):
        key = (kind or "").strip().lower()
        if key not in CONTEXTSPACE_DOC_KINDS:
            raise HTTPException(status_code=400, detail="invalid contextspace doc kind")
        repo_root = request.app.state.engine.repo_root
        write_contextspace_doc(repo_root, key, payload.content)
        return _contextspace_payload(repo_root)

    @router.post("/contextspace/spec/ingest", response_model=SpecIngestTicketsResponse)
    def ingest_contextspace_spec(request: Request):
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


__all__ = ["build_contextspace_routes"]
