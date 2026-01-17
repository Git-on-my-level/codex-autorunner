"""
Document management routes: read/write docs and chat functionality.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core.doc_chat import (
    DocChatBusyError,
    DocChatConflictError,
    DocChatError,
    DocChatValidationError,
    _normalize_kind,
)
from ..core.snapshot import (
    SnapshotError,
    load_snapshot,
    load_snapshot_state,
)
from ..core.usage import (
    UsageError,
    default_codex_home,
    get_repo_usage_series_cached,
    get_repo_usage_summary_cached,
    parse_iso_datetime,
)
from ..core.utils import atomic_write
from ..spec_ingest import (
    SpecIngestError,
    clear_work_docs,
)
from ..web.schemas import (
    DocChatPayload,
    DocContentRequest,
    DocsResponse,
    DocWriteResponse,
    IngestSpecRequest,
    IngestSpecResponse,
    RepoUsageResponse,
    SnapshotCreateResponse,
    SnapshotRequest,
    SnapshotResponse,
    UsageSeriesResponse,
)
from .shared import SSE_HEADERS


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
        snapshot_service = request.app.state.snapshot_service
        try:
            result = await snapshot_service.generate_snapshot()
        except SnapshotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "content": result.content,
            "truncated": result.truncated,
            "state": result.state,
        }

    async def _handle_doc_chat_request(
        request: Request,
        *,
        kind: Optional[str],
        payload: Optional[DocChatPayload],
    ):
        doc_chat = request.app.state.doc_chat
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        try:
            payload_dict = payload.model_dump(exclude_none=True) if payload else None
            doc_req = doc_chat.parse_request(payload_dict, kind=kind)
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if doc_chat.doc_busy():
            raise HTTPException(
                status_code=409,
                detail="Doc chat already running",
            )

        if doc_req.stream:
            return StreamingResponse(
                doc_chat.stream(doc_req),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        try:
            async with doc_chat.doc_lock():
                result = await doc_chat.execute(doc_req)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if result.get("status") != "ok":
            detail = result.get("detail") or "Doc chat failed"
            raise HTTPException(status_code=500, detail=detail)
        return result

    @router.post("/api/docs/chat")
    async def chat_docs(request: Request, payload: Optional[DocChatPayload] = None):
        return await _handle_doc_chat_request(request, kind=None, payload=payload)

    @router.post("/api/docs/{kind}/chat")
    async def chat_doc(
        kind: str, request: Request, payload: Optional[DocChatPayload] = None
    ):
        return await _handle_doc_chat_request(request, kind=kind, payload=payload)

    @router.post("/api/docs/chat/interrupt")
    async def interrupt_chat(request: Request):
        doc_chat = request.app.state.doc_chat
        try:
            return await doc_chat.interrupt()
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/docs/{kind}/chat/interrupt")
    async def interrupt_chat_kind(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        try:
            return await doc_chat.interrupt(kind)
        except DocChatValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/docs/{kind}/chat/apply")
    async def apply_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)

        try:
            async with doc_chat.doc_lock(key):
                pending = doc_chat.pending_patch(key)
                content = doc_chat.apply_saved_patch(key)
        except DocChatBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DocChatConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DocChatError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "status": "ok",
            "kind": key,
            "content": content,
            "agent_message": (pending or {}).get("agent_message")
            or f"Updated {key.upper()} via doc chat.",
            "created_at": (pending or {}).get("created_at"),
            "base_hash": (pending or {}).get("base_hash"),
        }

    @router.post("/api/docs/{kind}/chat/discard")
    async def discard_chat_patch(kind: str, request: Request):
        doc_chat = request.app.state.doc_chat
        key = _normalize_kind(kind)
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
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
        repo_blocked = doc_chat.repo_blocked_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        pending = doc_chat.pending_patch(key)
        if not pending:
            raise HTTPException(status_code=404, detail="No pending patch")
        return pending

    @router.post("/api/ingest-spec", response_model=IngestSpecResponse)
    async def ingest_spec(
        request: Request, payload: Optional[IngestSpecRequest] = None
    ):
        engine = request.app.state.engine
        repo_blocked = engine.repo_busy_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        spec_ingest = request.app.state.spec_ingest
        force = False
        spec_override: Optional[Path] = None
        message: Optional[str] = None
        agent: Optional[str] = None
        model: Optional[str] = None
        reasoning: Optional[str] = None
        if payload:
            force = payload.force
            if payload.spec_path:
                spec_override = Path(str(payload.spec_path))
            message = payload.message
            agent = payload.agent
            model = payload.model
            reasoning = payload.reasoning
        try:
            docs = await spec_ingest.execute(
                force=force,
                spec_path=spec_override,
                message=message,
                agent=agent,
                model=model,
                reasoning=reasoning,
            )
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return docs

    @router.post("/api/ingest-spec/interrupt", response_model=IngestSpecResponse)
    async def ingest_spec_interrupt(request: Request):
        spec_ingest = request.app.state.spec_ingest
        try:
            docs = await spec_ingest.interrupt()
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return docs

    @router.get("/api/ingest-spec/pending", response_model=IngestSpecResponse)
    def ingest_spec_pending(request: Request):
        engine = request.app.state.engine
        repo_blocked = engine.repo_busy_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        spec_ingest = request.app.state.spec_ingest
        try:
            pending = spec_ingest.pending_patch()
            if not pending:
                raise HTTPException(
                    status_code=404, detail="No pending spec ingest patch"
                )
            return pending
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/ingest-spec/apply", response_model=IngestSpecResponse)
    def ingest_spec_apply(request: Request):
        engine = request.app.state.engine
        repo_blocked = engine.repo_busy_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        spec_ingest = request.app.state.spec_ingest
        try:
            return spec_ingest.apply_patch()
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/ingest-spec/discard", response_model=IngestSpecResponse)
    def ingest_spec_discard(request: Request):
        engine = request.app.state.engine
        repo_blocked = engine.repo_busy_reason()
        if repo_blocked:
            raise HTTPException(status_code=409, detail=repo_blocked)
        spec_ingest = request.app.state.spec_ingest
        try:
            return spec_ingest.discard_patch()
        except SpecIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        summary, status = get_repo_usage_summary_cached(
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
            "status": status,
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
