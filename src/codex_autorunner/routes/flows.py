import json
import logging
import re
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import IO, Dict, Optional, Tuple, Union
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core.engine import Engine
from ..core.flows import (
    FlowController,
    FlowDefinition,
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
)
from ..core.flows.worker_process import (
    FlowWorkerHealth,
    check_worker_health,
    clear_worker_metadata,
    spawn_flow_worker,
)
from ..core.utils import find_repo_root
from ..flows.ticket_flow import build_ticket_flow_definition
from ..tickets import AgentPool
from ..tickets.files import list_ticket_paths, read_ticket, safe_relpath
from ..tickets.outbox import parse_user_message, resolve_outbox_paths

_logger = logging.getLogger(__name__)

_active_workers: Dict[
    str, Tuple[Optional[subprocess.Popen], Optional[IO[bytes]], Optional[IO[bytes]]]
] = {}
_controller_cache: Dict[tuple[Path, str], FlowController] = {}
_definition_cache: Dict[tuple[Path, str], FlowDefinition] = {}
_supported_flow_types = ("ticket_flow",)


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _require_flow_store(repo_root: Path) -> Optional[FlowStore]:
    db_path, _ = _flow_paths(repo_root)
    store = FlowStore(db_path)
    try:
        store.initialize()
        return store
    except Exception as exc:
        _logger.warning("Flows database unavailable at %s: %s", db_path, exc)
        return None


def _safe_list_flow_runs(
    repo_root: Path, flow_type: Optional[str] = None
) -> list[FlowRunRecord]:
    db_path, _ = _flow_paths(repo_root)
    store = FlowStore(db_path)
    try:
        store.initialize()
        records = store.list_flow_runs(flow_type=flow_type)
        return records
    except Exception as exc:
        _logger.debug("FlowStore list runs failed: %s", exc)
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass


def _build_flow_definition(repo_root: Path, flow_type: str) -> FlowDefinition:
    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    if key in _definition_cache:
        return _definition_cache[key]

    if flow_type == "ticket_flow":
        engine = Engine(repo_root)
        agent_pool = AgentPool(engine.config)
        definition = build_ticket_flow_definition(agent_pool=agent_pool)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")

    definition.validate()
    _definition_cache[key] = definition
    return definition


def _get_flow_controller(repo_root: Path, flow_type: str) -> FlowController:
    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    if key in _controller_cache:
        return _controller_cache[key]

    db_path, artifacts_root = _flow_paths(repo_root)
    definition = _build_flow_definition(repo_root, flow_type)

    controller = FlowController(
        definition=definition,
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    try:
        controller.initialize()
    except Exception as exc:
        _logger.warning("Failed to initialize flow controller: %s", exc)
        raise HTTPException(
            status_code=503, detail="Flows unavailable; initialize the repo first."
        ) from exc
    _controller_cache[key] = controller
    return controller


def _get_flow_record(repo_root: Path, run_id: str) -> FlowRunRecord:
    store = _require_flow_store(repo_root)
    if store is None:
        raise HTTPException(status_code=503, detail="Flows database unavailable")
    try:
        record = store.get_flow_run(run_id)
    finally:
        try:
            store.close()
        except Exception:
            pass
    if not record:
        raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")
    return record


def _active_or_paused_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    return next(
        (
            rec
            for rec in records
            if rec.status
            in (
                FlowRunStatus.RUNNING,
                FlowRunStatus.PAUSED,
            )
        ),
        None,
    )


def _normalize_run_id(run_id: Union[str, uuid.UUID]) -> str:
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id") from None


def _cleanup_worker_handle(run_id: str) -> None:
    handle = _active_workers.pop(run_id, None)
    if not handle:
        return

    proc, stdout, stderr = handle
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass

    for stream in (stdout, stderr):
        if stream and not stream.closed:
            try:
                stream.flush()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


def _reap_dead_worker(run_id: str) -> None:
    handle = _active_workers.get(run_id)
    if not handle:
        return
    proc, *_ = handle
    if proc and proc.poll() is not None:
        _cleanup_worker_handle(run_id)


def _ensure_worker_not_stale(health: FlowWorkerHealth) -> None:
    # Clear metadata if stale to allow clean respawn.
    if health.status in {"dead", "mismatch", "invalid"}:
        try:
            clear_worker_metadata(health.artifact_path.parent)
        except Exception:
            _logger.debug("Failed to clear worker metadata: %s", health.artifact_path)


class FlowStartRequest(BaseModel):
    input_data: Dict = Field(default_factory=dict)
    metadata: Optional[Dict] = None


class FlowStatusResponse(BaseModel):
    id: str
    flow_type: str
    status: str
    current_step: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    error_message: Optional[str]
    state: Dict = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: FlowRunRecord) -> "FlowStatusResponse":
        return cls(
            id=record.id,
            flow_type=record.flow_type,
            status=record.status.value,
            current_step=record.current_step,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error_message=record.error_message,
            state=record.state,
        )


class FlowArtifactInfo(BaseModel):
    id: str
    kind: str
    path: str
    created_at: str
    metadata: Dict = Field(default_factory=dict)


def _start_flow_worker(repo_root: Path, run_id: str) -> Optional[subprocess.Popen]:
    normalized_run_id = _normalize_run_id(run_id)

    health = check_worker_health(repo_root, normalized_run_id)
    _ensure_worker_not_stale(health)
    if health.is_alive:
        _logger.info(
            "Worker already active for run %s (pid=%s), skipping spawn",
            normalized_run_id,
            health.pid,
        )
        return None

    _reap_dead_worker(normalized_run_id)

    proc, stdout_handle, stderr_handle = spawn_flow_worker(repo_root, normalized_run_id)
    _active_workers[normalized_run_id] = (proc, stdout_handle, stderr_handle)
    _logger.info("Started flow worker for run %s (pid=%d)", normalized_run_id, proc.pid)
    return proc


def _stop_worker(run_id: str, timeout: float = 10.0) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    handle = _active_workers.get(normalized_run_id)
    if not handle:
        health = check_worker_health(find_repo_root(), normalized_run_id)
        if health.is_alive and health.pid:
            try:
                _logger.info(
                    "Stopping untracked worker for run %s (pid=%s)",
                    normalized_run_id,
                    health.pid,
                )
                subprocess.run(["kill", str(health.pid)], check=False)
            except Exception as exc:
                _logger.warning(
                    "Failed to stop untracked worker %s: %s", normalized_run_id, exc
                )
        return

    proc, *_ = handle
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _logger.warning(
                "Worker for run %s did not exit in time, killing", normalized_run_id
            )
            proc.kill()
        except Exception as exc:
            _logger.warning("Error stopping worker %s: %s", normalized_run_id, exc)

    _cleanup_worker_handle(normalized_run_id)


def build_flow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/flows", tags=["flows"])

    def _definition_info(definition: FlowDefinition) -> Dict:
        return {
            "type": definition.flow_type,
            "name": definition.name,
            "description": definition.description,
            "input_schema": definition.input_schema or {},
        }

    def _resolve_outbox_for_record(record: FlowRunRecord, repo_root: Path):
        workspace_root = Path(record.input_data.get("workspace_root") or repo_root)
        runs_dir = Path(record.input_data.get("runs_dir") or ".codex-autorunner/runs")
        return resolve_outbox_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=record.id
        )

    @router.get("")
    async def list_flow_definitions():
        repo_root = find_repo_root()
        definitions = [
            _definition_info(_build_flow_definition(repo_root, flow_type))
            for flow_type in _supported_flow_types
        ]
        return {"definitions": definitions}

    @router.get("/runs", response_model=list[FlowStatusResponse])
    async def list_runs(flow_type: Optional[str] = None):
        repo_root = find_repo_root()
        records = _safe_list_flow_runs(repo_root, flow_type=flow_type)
        return [FlowStatusResponse.from_record(rec) for rec in records]

    @router.get("/{flow_type}")
    async def get_flow_definition(flow_type: str):
        repo_root = find_repo_root()
        if flow_type not in _supported_flow_types:
            raise HTTPException(
                status_code=404, detail=f"Unknown flow type: {flow_type}"
            )
        definition = _build_flow_definition(repo_root, flow_type)
        return _definition_info(definition)

    async def _start_flow(
        flow_type: str, request: FlowStartRequest, *, force_new: bool = False
    ) -> FlowStatusResponse:
        if flow_type not in _supported_flow_types:
            raise HTTPException(
                status_code=404, detail=f"Unknown flow type: {flow_type}"
            )

        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root, flow_type)

        # Reuse an active/paused run unless force_new is requested.
        if not force_new:
            runs = controller.list_runs()
            active = _active_or_paused_run(runs)
            if active:
                _reap_dead_worker(active.id)
                _start_flow_worker(repo_root, active.id)
                response = FlowStatusResponse.from_record(active)
                response.state = response.state or {}
                response.state["hint"] = "active_run_reused"
                return response

        run_id = _normalize_run_id(uuid.uuid4())

        record = await controller.start_flow(
            input_data=request.input_data,
            run_id=run_id,
            metadata=request.metadata,
        )

        _start_flow_worker(repo_root, run_id)

        return FlowStatusResponse.from_record(record)

    @router.post("/{flow_type}/start", response_model=FlowStatusResponse)
    async def start_flow(flow_type: str, request: FlowStartRequest):
        meta = request.metadata if isinstance(request.metadata, dict) else {}
        force_new = bool(meta.get("force_new"))
        return await _start_flow(flow_type, request, force_new=force_new)

    @router.post("/ticket_flow/bootstrap", response_model=FlowStatusResponse)
    async def bootstrap_ticket_flow(request: Optional[FlowStartRequest] = None):
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        flow_request = request or FlowStartRequest()
        meta = flow_request.metadata if isinstance(flow_request.metadata, dict) else {}
        force_new = bool(meta.get("force_new"))

        if not force_new:
            records = _safe_list_flow_runs(repo_root, flow_type="ticket_flow")
            active = next(
                (
                    rec
                    for rec in records
                    if rec.status
                    in (
                        FlowRunStatus.RUNNING,
                        FlowRunStatus.PAUSED,
                    )
                ),
                None,
            )
            if active:
                _reap_dead_worker(active.id)
                _start_flow_worker(repo_root, active.id)
                resp = FlowStatusResponse.from_record(active)
                resp.state = resp.state or {}
                resp.state["hint"] = "active_run_reused"
                return resp

        seeded = False
        if not ticket_path.exists():
            template = """---
agent: codex
done: false
title: Bootstrap ticket plan
goal: Create SPEC and seed follow-up tickets
requires:
  - .codex-autorunner/ISSUE.md
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md` (or ask for the issue/PR URL if missing).
- Create or update `.codex-autorunner/SPEC.md` that captures goals, scope, risks, and constraints.
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/handoff/` if needed.
- Write `USER_MESSAGE.md` with `mode: pause` summarizing the ticket plan and requesting user review before proceeding.
"""
            ticket_path.write_text(template, encoding="utf-8")
            seeded = True

        meta = flow_request.metadata if isinstance(flow_request.metadata, dict) else {}
        payload = FlowStartRequest(
            input_data=flow_request.input_data,
            metadata=meta | {"seeded_ticket": seeded},
        )
        return await _start_flow("ticket_flow", payload, force_new=force_new)

    @router.get("/ticket_flow/tickets")
    async def list_ticket_files():
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        tickets = []
        for path in list_ticket_paths(ticket_dir):
            doc, errors = read_ticket(path)
            rel_path = safe_relpath(path, repo_root)
            tickets.append(
                {
                    "path": rel_path,
                    "index": getattr(doc, "index", None),
                    "frontmatter": asdict(doc.frontmatter) if doc else None,
                    "body": doc.body if doc else None,
                    "errors": errors,
                }
            )
        return {
            "ticket_dir": safe_relpath(ticket_dir, repo_root),
            "tickets": tickets,
        }

    @router.post("/{run_id}/stop", response_model=FlowStatusResponse)
    async def stop_flow(run_id: uuid.UUID):
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type)

        _stop_worker(run_id)

        updated = await controller.stop_flow(run_id)
        return FlowStatusResponse.from_record(updated)

    @router.post("/{run_id}/resume", response_model=FlowStatusResponse)
    async def resume_flow(run_id: uuid.UUID):
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type)

        updated = await controller.resume_flow(run_id)
        _reap_dead_worker(run_id)
        _start_flow_worker(repo_root, run_id)

        return FlowStatusResponse.from_record(updated)

    @router.get("/{run_id}/status", response_model=FlowStatusResponse)
    async def get_flow_status(run_id: uuid.UUID):
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()

        _reap_dead_worker(run_id)

        record = _get_flow_record(repo_root, run_id)

        # If the worker died but metadata still claims it exists, clear it so status
        # callers get a clean view next time they start/resume.
        health = check_worker_health(repo_root, run_id)
        _ensure_worker_not_stale(health)

        return FlowStatusResponse.from_record(record)

    @router.get("/{run_id}/events")
    async def stream_flow_events(run_id: uuid.UUID, after: Optional[int] = None):
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type)

        async def event_stream():
            try:
                async for event in controller.stream_events(run_id, after_seq=after):
                    data = event.model_dump(mode="json")
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
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

    @router.get("/{run_id}/handoff_history")
    async def get_handoff_history(run_id: str):
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        paths = _resolve_outbox_for_record(record, repo_root)

        history_entries = []
        history_dir = paths.handoff_history_dir
        if history_dir.exists() and history_dir.is_dir():
            for entry in sorted(
                [p for p in history_dir.iterdir() if p.is_dir()],
                key=lambda p: p.name,
                reverse=True,
            ):
                msg_path = entry / "USER_MESSAGE.md"
                message, errors = (
                    parse_user_message(msg_path)
                    if msg_path.exists()
                    else (None, ["USER_MESSAGE.md missing"])
                )
                msg_dict = asdict(message) if message else None
                attachments = []
                for child in sorted(entry.rglob("*")):
                    if child.name == "USER_MESSAGE.md":
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
                            "url": f"/api/flows/{normalized}/handoff_history/{entry.name}/{quote(rel)}",
                        }
                    )
                history_entries.append(
                    {
                        "seq": entry.name,
                        "message": msg_dict,
                        "errors": errors,
                        "attachments": attachments,
                        "path": safe_relpath(entry, repo_root),
                    }
                )

        return {"run_id": normalized, "history": history_entries}

    @router.get("/{run_id}/handoff_history/{seq}/{file_path:path}")
    async def get_handoff_file(run_id: str, seq: str, file_path: str):
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        paths = _resolve_outbox_for_record(record, repo_root)

        base_history = paths.handoff_history_dir.resolve()

        seq_clean = seq.strip()
        if not re.fullmatch(r"[0-9]{4}", seq_clean):
            raise HTTPException(
                status_code=400, detail="Invalid handoff history sequence"
            )

        history_dir = (base_history / seq_clean).resolve()
        if not history_dir.is_relative_to(base_history) or not history_dir.is_dir():
            raise HTTPException(
                status_code=404, detail=f"Handoff history not found for run {run_id}"
            )

        file_rel = PurePosixPath(file_path)
        if file_rel.is_absolute() or ".." in file_rel.parts or "\\" in file_path:
            raise HTTPException(status_code=400, detail="Invalid handoff file path")

        safe_parts = [part for part in file_rel.parts if part not in {"", "."}]
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in safe_parts):
            raise HTTPException(status_code=400, detail="Invalid handoff file path")

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
                detail="Access denied: file outside handoff history directory",
            )

        return FileResponse(resolved, filename=resolved.name)

    @router.get("/{run_id}/artifacts", response_model=list[FlowArtifactInfo])
    async def list_flow_artifacts(run_id: str):
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        controller = _get_flow_controller(repo_root, record.flow_type)

        artifacts = controller.get_artifacts(normalized)
        return [
            FlowArtifactInfo(
                id=art.id,
                kind=art.kind,
                path=art.path,
                created_at=art.created_at,
                metadata=art.metadata,
            )
            for art in artifacts
        ]

    @router.get("/{run_id}/artifact")
    async def get_flow_artifact(run_id: str, kind: Optional[str] = None):
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        controller = _get_flow_controller(repo_root, record.flow_type)

        artifacts_root = controller.get_artifacts_dir(normalized)
        if not artifacts_root:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404, detail=f"Artifact directory not found for run {run_id}"
            )

        artifacts = controller.get_artifacts(normalized)

        if kind:
            matching = [a for a in artifacts if a.kind == kind]
        else:
            matching = artifacts

        if not matching:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404,
                detail=f"No artifact found for run {run_id} with kind={kind}",
            )

        artifact = matching[0]
        artifact_path = Path(artifact.path)

        if not artifact_path.exists():
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404, detail=f"Artifact file not found: {artifact.path}"
            )

        if not artifact_path.resolve().is_relative_to(artifacts_root.resolve()):
            from fastapi import HTTPException

            raise HTTPException(
                status_code=403,
                detail="Access denied: artifact path outside run directory",
            )

        return FileResponse(artifact_path, filename=artifact_path.name)

    return router
