from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ....contextspace.paths import (
    CONTEXTSPACE_DOC_KINDS,
    contextspace_dir,
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

    def _contextspace_tree_payload(repo_root):
        base = contextspace_dir(repo_root)
        base.mkdir(parents=True, exist_ok=True)
        pinned_paths = {entry["path"] for entry in serialize_contextspace_doc_catalog()}

        def _serialize_node(path: Path) -> dict[str, object]:
            rel_path = path.relative_to(base).as_posix()
            stat = path.stat()
            modified_at = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
            if path.is_dir():
                children = [
                    _serialize_node(child)
                    for child in sorted(
                        path.iterdir(),
                        key=lambda child: (
                            child.is_file(),
                            child.name.lower(),
                        ),
                    )
                ]
                return {
                    "name": path.name,
                    "path": rel_path,
                    "type": "folder",
                    "modified_at": modified_at,
                    "size": None,
                    "is_pinned": False,
                    "children": children,
                }
            return {
                "name": path.name,
                "path": rel_path,
                "type": "file",
                "modified_at": modified_at,
                "size": stat.st_size,
                "is_pinned": rel_path in pinned_paths,
            }

        tree = [
            _serialize_node(child)
            for child in sorted(
                base.iterdir(),
                key=lambda child: (
                    child.is_file(),
                    0 if child.name in pinned_paths else 1,
                    child.name.lower(),
                ),
            )
        ]
        return {"tree": tree, "defaultPath": "active_context.md"}

    @router.get("/contextspace", response_model=ContextspaceResponse)
    def get_contextspace(request: Request):
        repo_root = request.app.state.engine.repo_root
        return _contextspace_payload(repo_root)

    @router.get("/contextspace/tree")
    def get_contextspace_tree(request: Request):
        repo_root = request.app.state.engine.repo_root
        return _contextspace_tree_payload(repo_root)

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
