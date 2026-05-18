from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    ContextspaceResponse,
    ContextspaceWriteRequest,
    SpecIngestTicketsResponse,
)
from ..services.workspace_resources import (
    ContextspaceResourceService,
    WorkspaceResourceError,
)


def _raise_http(exc: WorkspaceResourceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def build_contextspace_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["contextspace"])
    service = ContextspaceResourceService()

    @router.get("/contextspace", response_model=ContextspaceResponse)
    def get_contextspace(request: Request):
        repo_root = request.app.state.engine.repo_root
        return service.payload(repo_root)

    @router.get("/contextspace/tree")
    def get_contextspace_tree(request: Request):
        repo_root = request.app.state.engine.repo_root
        return service.tree_payload(repo_root)

    @router.put("/contextspace/{kind}", response_model=ContextspaceResponse)
    def put_contextspace(
        kind: str, payload: ContextspaceWriteRequest, request: Request
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            return service.write_document(repo_root, kind, payload.content)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    @router.post("/contextspace/spec/ingest", response_model=SpecIngestTicketsResponse)
    def ingest_contextspace_spec(request: Request):
        repo_root = request.app.state.engine.repo_root
        try:
            return service.ingest_spec(repo_root)
        except WorkspaceResourceError as exc:
            _raise_http(exc)

    return router


__all__ = ["build_contextspace_routes"]
