from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Optional, cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .....core.flows.workspace_root import resolve_ticket_flow_workspace_root
from .run_list_cache import list_flow_runs_payload

if TYPE_CHECKING:
    from . import FlowRouteDependencies, FlowRoutesState

_logger = logging.getLogger(__name__)
_WorkerHandle = tuple[Optional[subprocess.Popen[Any]], Any, Any]


def _reap_dead_worker(run_id: str, state: "FlowRoutesState") -> None:
    with state.lock:
        handle = cast(Optional[_WorkerHandle], state.active_workers.get(run_id))
    if not handle:
        return
    proc, *_ = handle
    if proc and proc.poll() is not None:
        from .runtime_service import cleanup_worker_handle

        cleanup_worker_handle(run_id, state)


def _normalize_run_id(run_id: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id") from None


def _resolve_outbox_for_record(record: Any, repo_root: Path):
    from .....tickets.outbox import resolve_outbox_paths

    input_data = dict(getattr(record, "input_data", {}) or {})
    try:
        workspace_root = resolve_ticket_flow_workspace_root(input_data, repo_root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return resolve_outbox_paths(workspace_root=workspace_root, run_id=record.id)


def build_status_history_routes(
    deps: "FlowRouteDependencies", *, prefix: str = "/api/flows"
) -> tuple[APIRouter, list[str]]:
    router = APIRouter(prefix=prefix, tags=["flows"])
    from .runtime_service import load_flow_run_record, load_flow_run_records

    def _ensure_state_in_app(request: Request) -> "FlowRoutesState":
        from typing import cast

        if not hasattr(request.app.state, "flow_routes_state"):
            from . import FlowRoutesState

            request.app.state.flow_routes_state = FlowRoutesState()
        return cast("FlowRoutesState", request.app.state.flow_routes_state)

    @router.get("/runs", response_model=list[Any])
    async def list_runs(
        request: Request,
        flow_type: Optional[str] = None,
        flow_target_id: Optional[str] = None,
        reconcile: bool = False,
    ):
        _ensure_state_in_app(request)
        return list_flow_runs_payload(
            request,
            deps,
            load_flow_run_records,
            flow_type=flow_type,
            flow_target_id=flow_target_id,
            reconcile=reconcile,
        )

    @router.get("/{run_id}/status", response_model=dict[str, Any])
    async def get_flow_status(
        http_request: Request, run_id: str, reconcile: bool = False
    ):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=404, detail="Repository not found")
        _reap_dead_worker(run_id, state)
        store = deps.require_flow_store(repo_root)
        try:
            record = load_flow_run_record(
                repo_root,
                run_id,
                flow_type="ticket_flow",
                reconcile=reconcile,
                store=store,
                build_flow_orchestration_service_fn=deps.build_flow_orchestration_service,
            )
            if record is None:
                raise HTTPException(
                    status_code=404, detail=f"Flow run {run_id} not found"
                )
            return deps.build_flow_status_response(record, repo_root, store=store)
        finally:
            if store:
                store.close()

    @router.get("/{run_id}/events")
    async def stream_flow_events(
        http_request: Request, run_id: str, after: Optional[int] = None
    ):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=404, detail="Repository not found")

        record = deps.get_flow_record(repo_root, run_id)
        controller = deps.get_flow_controller(repo_root, record.flow_type, state)

        async def event_stream():
            try:
                resume_after = after
                if resume_after is None:
                    last_event_id = http_request.headers.get("Last-Event-ID")
                    if last_event_id:
                        try:
                            resume_after = int(last_event_id)
                        except ValueError:
                            _logger.debug(
                                "Invalid Last-Event-ID %s for run %s",
                                last_event_id,
                                run_id,
                            )
                async for event in controller.stream_events(
                    run_id, after_seq=resume_after
                ):
                    data = event.model_dump(mode="json")
                    yield f"id: {event.seq}\ndata: {json.dumps(data)}\n\n"
            except Exception as e:  # intentional: SSE generator, log and propagate
                _logger.exception("Error streaming events for run %s: %s", run_id, e)
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/{run_id}/dispatch_history")
    async def get_dispatch_history(http_request: Request, run_id: str):
        normalized = _normalize_run_id(run_id)
        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=404, detail="Repository not found")

        record = deps.get_flow_record(repo_root, normalized)
        from .history_artifacts import (
            get_diff_stats_by_dispatch_seq as _get_diff_stats_by_dispatch_seq,
        )

        outbox_paths = _resolve_outbox_for_record(record, repo_root)
        diff_by_seq = _get_diff_stats_by_dispatch_seq(repo_root, record, outbox_paths)

        from .....tickets.files import safe_relpath
        from .....tickets.outbox import parse_dispatch

        history_dir = outbox_paths.dispatch_history_dir

        history_entries = []
        if history_dir.exists() and history_dir.is_dir():
            for entry in sorted(
                [p for p in history_dir.iterdir() if p.is_dir()],
                key=lambda p: p.name,
                reverse=True,
            ):
                dispatch_path = entry / "DISPATCH.md"
                dispatch, errors = (
                    parse_dispatch(dispatch_path)
                    if dispatch_path.exists()
                    else (None, ["Dispatch file missing"])
                )
                if dispatch is not None:
                    dispatch_dict = asdict(dispatch)
                    dispatch_dict["is_handoff"] = dispatch.is_handoff
                    try:
                        entry_seq_int = int(entry.name)
                    except (ValueError, TypeError):
                        entry_seq_int = 0
                    if entry_seq_int and entry_seq_int in diff_by_seq:
                        dispatch_dict["diff_stats"] = diff_by_seq[entry_seq_int]
                else:
                    dispatch_dict = None
                attachments = []
                for child in sorted(entry.rglob("*")):
                    if child.name == "DISPATCH.md":
                        continue
                    rel = child.relative_to(entry).as_posix()
                    if any(part.startswith(".") for part in Path(rel).parts):
                        continue
                    if child.is_dir():
                        continue
                    attachments.append(
                        {
                            "name": child.name,
                            "rel_path": rel,
                            "path": safe_relpath(child, repo_root),
                            "size": child.stat().st_size if child.is_file() else None,
                            "url": f"api/flows/{normalized}/dispatch_history/{entry.name}/{quote(rel)}",
                        }
                    )
                created_at: Optional[str] = None
                for _cand in (dispatch_path, entry):
                    try:
                        created_at = datetime.fromtimestamp(
                            _cand.stat().st_mtime, tz=timezone.utc
                        ).isoformat()
                        break
                    except OSError:
                        continue
                history_entries.append(
                    {
                        "seq": entry.name,
                        "dispatch": dispatch_dict,
                        "errors": errors,
                        "attachments": attachments,
                        "path": safe_relpath(entry, repo_root),
                        "created_at": created_at,
                    }
                )

        return {"run_id": normalized, "history": history_entries}

    @router.get("/{run_id}/dispatch_history/{seq}/{file_path:path}")
    async def get_dispatch_file(run_id: str, seq: str, file_path: str):
        """Get an attachment file from a dispatch history entry."""
        normalized = _normalize_run_id(run_id)
        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=404, detail="Repository not found")

        record = deps.get_flow_record(repo_root, normalized)
        base_history = _resolve_outbox_for_record(
            record, repo_root
        ).dispatch_history_dir.resolve()

        seq_clean = seq.strip()
        if not re.fullmatch(r"[0-9]{4}", seq_clean):
            raise HTTPException(
                status_code=400, detail="Invalid dispatch history sequence"
            )

        history_dir = (base_history / seq_clean).resolve()
        if not history_dir.is_relative_to(base_history) or not history_dir.is_dir():
            raise HTTPException(
                status_code=404, detail=f"Dispatch history not found for run {run_id}"
            )

        file_rel = PurePosixPath(file_path)
        if file_rel.is_absolute() or ".." in file_rel.parts or "\\" in file_path:
            raise HTTPException(status_code=400, detail="Invalid dispatch file path")

        safe_parts = [part for part in file_rel.parts if part not in {"", "."}]
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in safe_parts):
            raise HTTPException(status_code=400, detail="Invalid dispatch file path")

        target = (history_dir / Path(*safe_parts)).resolve()
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not resolved.exists():
            raise HTTPException(status_code=404, detail="File not found")

        if not resolved.is_relative_to(history_dir):
            raise HTTPException(
                status_code=403,
                detail="Access denied: file outside dispatch history directory",
            )

        return FileResponse(resolved, filename=resolved.name)

    @router.get("/{run_id}/reply_history/{seq}/{file_path:path}")
    def get_reply_history_file(run_id: str, seq: str, file_path: str):
        from .....core.safe_paths import SafePathError, validate_single_filename

        repo_root = deps.find_repo_root()
        if repo_root is None:
            raise HTTPException(status_code=404, detail="Repository not found")

        from .runtime_service import flow_paths

        db_path, _ = flow_paths(repo_root)
        from .....core.flows import FlowStore

        store = FlowStore(db_path)
        try:
            store.initialize()
            record = store.get_flow_run(run_id)
        finally:
            try:
                store.close()
            except (OSError, RuntimeError):
                _logger.debug(
                    "Failed to close flow store in reply history", exc_info=True
                )
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")

        if not (len(seq) == 4 and seq.isdigit()):
            raise HTTPException(status_code=400, detail="Invalid seq")

        try:
            filename = validate_single_filename(file_path)
        except SafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        input_data = dict(record.input_data or {})
        try:
            workspace_root = resolve_ticket_flow_workspace_root(input_data, repo_root)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        from .....tickets.replies import resolve_reply_paths

        reply_paths = resolve_reply_paths(workspace_root=workspace_root, run_id=run_id)
        target = reply_paths.reply_history_dir / seq / filename
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path=str(target), filename=filename)

    return router, ["status_history"]


_STATUS_HISTORY_ROUTE_API = (
    "list_runs",
    "get_flow_status",
    "stream_flow_events",
    "get_dispatch_history",
    "get_dispatch_file",
    "get_reply_history_file",
)
