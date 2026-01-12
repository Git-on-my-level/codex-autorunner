"""
Document management routes: read/write docs and chat functionality.
"""

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core.doc_chat import (
    DocChatBusyError,
    DocChatError,
    DocChatValidationError,
    _normalize_kind,
)
from ..core.snapshot import (
    SnapshotError,
    generate_snapshot,
    load_snapshot,
    load_snapshot_state,
)
from ..core.usage import (
    UsageError,
    default_codex_home,
    get_repo_usage_series_cached,
    parse_iso_datetime,
    summarize_repo_usage,
)
from ..core.utils import atomic_write
from ..spec_ingest import (
    SpecIngestError,
    clear_work_docs,
    generate_docs_from_spec,
    write_ingested_docs,
)
from ..web.schemas import (
    DocChatPayload,
    DocContentRequest,
    DocsResponse,
    DocWriteResponse,
    IngestSpecRequest,
    RepoUsageResponse,
    SnapshotCreateResponse,
    SnapshotRequest,
    SnapshotResponse,
    UsageSeriesResponse,
)


def build_docs_routes() -> APIRouter:
    """Build routes for document management and chat."""
    router = APIRouter()

    @router.get("/api/docs", response_model=DocsResponse)
    def get_docs(request: Request):
        engine = request.app.state.engine
        return {
            "todo": engine.docs.read_doc("todo"),
            "progress": engine.docs.read_doc("progress"),
            "opinions": engine.docs.read_doc("opinions"),
            "spec": engine.docs.read_doc("spec"),
            "summary": engine.docs.read_doc("summary"),
        }

    @router.put("/api/docs/{kind}", response_model=DocWriteResponse)
    def put_doc(kind: str, payload: DocContentRequest, request: Request):
        engine = request.app.state.engine
        key = kind.lower()
        if key not in ("todo", "progress", "opinions", "spec", "summary"):
            raise HTTPException(status_code=400, detail="invalid doc kind")
        content = payload.content
        atomic_write(engine.config.doc_path(key), content)
        return {"kind": key, "content": content}

    @router.get("/api/snapshot", response_model=SnapshotResponse)
    def get_snapshot(request: Request):
        engine = request.app.state.engine
        content = load_snapshot(engine)
        state = load_snapshot_state(engine)
        return {"exists": bool(content), "content": content or "", "state": state or {}}

    @router.post("/api/snapshot", response_model=SnapshotCreateResponse)
    async def post_snapshot(
        request: Request, payload: Optional[SnapshotRequest] = None
    ):
        # Snapshot generation has a single default behavior now; we accept an
        # optional JSON object for backwards compatibility, but ignore any fields.
        engine = request.app.state.engine
        try:
            result = await asyncio.to_thread(
                generate_snapshot,
                engine,
            )
        except SnapshotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "content": result.content,
            "truncated": result.truncated,
            "state": result.state,
        }

    @router.post("/api/docs/{kind}/chat")
    async def chat_doc(
        kind: str, request: Request, payload: Optional[DocChatPayload] = None
    ):
        doc_chat = request.app.state.doc_chat
        try:
            payload_dict = payload.model_dump(exclude_none=True) if payload else None
            doc_req = doc_chat.parse_request(kind, payload_dict)
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        if doc_chat.doc_busy(doc_req.kind):
            raise HTTPException(
                status_code=409,
                detail=f"Doc chat already running for {doc_req.kind}",
            )

        if doc_req.stream:
            return StreamingResponse(
                doc_chat.stream(doc_req), media_type="text/event-stream"
            )

        try:
            async with doc_chat.doc_lock(doc_req.kind):
                result = await doc_chat.execute(doc_req)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if result.get("status") != "ok":
            detail = result.get("detail") or "Doc chat failed"
            raise HTTPException(status_code=500, detail=detail)
        return result

    @router.post("/api/docs/{kind}/chat/apply")
    async def apply_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        try:
            async with doc_chat.doc_lock(key):
                content = doc_chat.apply_saved_patch(key)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "status": "ok",
            "kind": key,
            "content": content,
            "agent_message": doc_chat.last_agent_message
            or f"Updated {key.upper()} via doc chat.",
        }

    @router.post("/api/docs/{kind}/chat/discard")
    async def discard_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        try:
            async with doc_chat.doc_lock(key):
                content = doc_chat.discard_patch(key)
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "ok", "kind": key, "content": content}

    @router.get("/api/docs/{kind}/chat/pending")
    async def pending_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        pending = doc_chat.pending_patch(key)
        if not pending:
            raise HTTPException(status_code=404, detail="No pending patch")
        return pending

    @router.post("/api/ingest-spec", response_model=DocsResponse)
    def ingest_spec(request: Request, payload: Optional[IngestSpecRequest] = None):
        engine = request.app.state.engine
        force = False
        spec_override: Optional[Path] = None
        if payload:
            force = payload.force
            if payload.spec_path:
                spec_override = Path(str(payload.spec_path))
        try:
            docs = generate_docs_from_spec(engine, spec_path=spec_override)
            write_ingested_docs(engine, docs, force=force)
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return docs

    @router.post("/api/docs/clear", response_model=DocsResponse)
    def clear_docs(request: Request):
        engine = request.app.state.engine
        try:
            docs = clear_work_docs(engine)
            docs["spec"] = engine.docs.read_doc("spec")
            docs["summary"] = engine.docs.read_doc("summary")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return docs

    @router.get("/api/usage", response_model=RepoUsageResponse)
    def get_usage(
        request: Request, since: Optional[str] = None, until: Optional[str] = None
    ):
        engine = request.app.state.engine
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        summary = summarize_repo_usage(
            engine.repo_root,
            default_codex_home(),
            since=since_dt,
            until=until_dt,
        )
        return {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            **summary.to_dict(),
        }

    @router.get("/api/usage/series", response_model=UsageSeriesResponse)
    def get_usage_series(
        request: Request,
        since: Optional[str] = None,
        until: Optional[str] = None,
        bucket: str = "day",
        segment: str = "none",
    ):
        engine = request.app.state.engine
        try:
            since_dt = parse_iso_datetime(since)
            until_dt = parse_iso_datetime(until)
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            series, status = get_repo_usage_series_cached(
                engine.repo_root,
                default_codex_home(),
                since=since_dt,
                until=until_dt,
                bucket=bucket,
                segment=segment,
            )
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "mode": "repo",
            "repo": str(engine.repo_root),
            "codex_home": str(default_codex_home()),
            "since": since,
            "until": until,
            "status": status,
            **series,
        }

    return router
