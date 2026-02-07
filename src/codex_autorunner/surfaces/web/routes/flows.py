import json
import logging
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Dict, Optional, Tuple, Union
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ....core.config import load_repo_config
from ....core.flows import (
    FlowController,
    FlowDefinition,
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
)
from ....core.flows.reconciler import reconcile_flow_run
from ....core.flows.ux_helpers import (
    bootstrap_check as ux_bootstrap_check,
)
from ....core.flows.ux_helpers import (
    build_flow_status_snapshot,
    ensure_worker,
    issue_md_path,
    seed_issue_from_github,
    seed_issue_from_text,
)
from ....core.flows.worker_process import FlowWorkerHealth, check_worker_health
from ....core.runtime import RuntimeContext
from ....core.safe_paths import SafePathError, validate_single_filename
from ....core.utils import atomic_write, find_repo_root
from ....flows.ticket_flow import build_ticket_flow_definition
from ....integrations.github.service import GitHubError, GitHubService
from ....tickets import AgentPool
from ....tickets.bulk import bulk_clear_model_pin, bulk_set_agent
from ....tickets.files import (
    list_ticket_paths,
    parse_ticket_index,
    read_ticket,
    safe_relpath,
)
from ....tickets.frontmatter import parse_markdown_frontmatter
from ....tickets.lint import lint_ticket_directory, lint_ticket_frontmatter
from ....tickets.outbox import parse_dispatch, resolve_outbox_paths
from ..schemas import (
    TicketBulkClearModelRequest,
    TicketBulkSetAgentRequest,
    TicketBulkUpdateResponse,
    TicketCreateRequest,
    TicketDeleteResponse,
    TicketResponse,
    TicketUpdateRequest,
)

_logger = logging.getLogger(__name__)

_supported_flow_types = ("ticket_flow",)


@dataclass
class FlowRoutesState:
    active_workers: Dict[
        str, Tuple[Optional[subprocess.Popen], Optional[IO[bytes]], Optional[IO[bytes]]]
    ]
    controller_cache: Dict[tuple[Path, str], FlowController]
    definition_cache: Dict[tuple[Path, str], FlowDefinition]
    lock: threading.Lock

    def __init__(self) -> None:
        self.active_workers = {}
        self.controller_cache = {}
        self.definition_cache = {}
        self.lock = threading.Lock()


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _ticket_dir(repo_root: Path) -> Path:
    repo_root = repo_root.resolve()
    return repo_root / ".codex-autorunner" / "tickets"


def _find_ticket_path_by_index(ticket_dir: Path, index: int) -> Optional[Path]:
    for path in list_ticket_paths(ticket_dir):
        idx = parse_ticket_index(path.name)
        if idx == index:
            return path
    return None


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
    repo_root: Path, flow_type: Optional[str] = None, *, recover_stuck: bool = False
) -> list[FlowRunRecord]:
    db_path, _ = _flow_paths(repo_root)
    store = FlowStore(db_path)
    try:
        store.initialize()
        records = store.list_flow_runs(flow_type=flow_type)
        if recover_stuck:
            # Recover any flows stuck in active states with dead workers
            records = [
                reconcile_flow_run(repo_root, rec, store, logger=_logger)[0]
                for rec in records
            ]
        return records
    except Exception as exc:
        _logger.debug("FlowStore list runs failed: %s", exc)
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass


def _build_flow_definition(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> FlowDefinition:
    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        if key in state.definition_cache:
            return state.definition_cache[key]

    if flow_type == "ticket_flow":
        config = load_repo_config(repo_root)
        engine = RuntimeContext(
            repo_root=repo_root,
            config=config,
        )
        agent_pool = AgentPool(engine.config)
        definition = build_ticket_flow_definition(agent_pool=agent_pool)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")

    definition.validate()
    with state.lock:
        state.definition_cache[key] = definition
    return definition


def _get_flow_controller(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> FlowController:
    repo_root = repo_root.resolve()
    key = (repo_root, flow_type)
    with state.lock:
        if key in state.controller_cache:
            return state.controller_cache[key]

    db_path, artifacts_root = _flow_paths(repo_root)
    definition = _build_flow_definition(repo_root, flow_type, state)

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
    with state.lock:
        state.controller_cache[key] = controller
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
    if not records:
        return None
    latest = records[0]
    if latest.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
        return latest
    return None


def _latest_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    if not records:
        return None
    return records[0]


def _normalize_run_id(run_id: Union[str, uuid.UUID]) -> str:
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id") from None


def _validate_tickets(ticket_dir: Path) -> list[str]:
    """Validate all tickets in the directory and return a list of error messages."""
    errors: list[str] = []

    if not ticket_dir.exists():
        return errors

    # Check for directory-level errors (duplicate indices)
    dir_errors = lint_ticket_directory(ticket_dir)
    errors.extend(dir_errors)

    # Check each ticket file for frontmatter errors
    ticket_paths = list_ticket_paths(ticket_dir)
    for path in ticket_paths:
        _, ticket_errors = read_ticket(path)
        for err in ticket_errors:
            errors.append(f"{path.relative_to(path.parent.parent)}: {err}")

    return errors


def _lint_after_ticket_update(ticket_dir: Path) -> list[str]:
    if not ticket_dir.exists():
        return []
    return _validate_tickets(ticket_dir)


def _cleanup_worker_handle(run_id: str, state: FlowRoutesState) -> None:
    with state.lock:
        handle = state.active_workers.pop(run_id, None)
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


def _reap_dead_worker(run_id: str, state: FlowRoutesState) -> None:
    with state.lock:
        handle = state.active_workers.get(run_id)
    if not handle:
        return
    proc, *_ = handle
    if proc and proc.poll() is not None:
        _cleanup_worker_handle(run_id, state)


class FlowStartRequest(BaseModel):
    input_data: Dict = Field(default_factory=dict)
    metadata: Optional[Dict] = None


class BootstrapCheckResponse(BaseModel):
    status: str
    github_available: Optional[bool] = None
    repo: Optional[str] = None


class SeedIssueRequest(BaseModel):
    issue_ref: Optional[str] = None  # GitHub issue number, #num, or URL
    plan_text: Optional[str] = None  # Freeform plan text when GitHub unavailable


class FlowWorkerHealthResponse(BaseModel):
    status: str
    pid: Optional[int]
    is_alive: bool
    message: Optional[str] = None

    @classmethod
    def from_health(cls, health: FlowWorkerHealth) -> "FlowWorkerHealthResponse":
        return cls(
            status=health.status,
            pid=health.pid,
            is_alive=health.is_alive,
            message=health.message,
        )


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
    reason_summary: Optional[str] = None
    ticket_progress: Optional[Dict[str, int]] = None
    last_event_seq: Optional[int] = None
    last_event_at: Optional[str] = None
    worker_health: Optional[FlowWorkerHealthResponse] = None

    @classmethod
    def from_record(
        cls,
        record: FlowRunRecord,
        *,
        last_event_seq: Optional[int] = None,
        last_event_at: Optional[str] = None,
        worker_health: Optional[FlowWorkerHealth] = None,
    ) -> "FlowStatusResponse":
        state = record.state or {}
        reason_summary = None
        if isinstance(state, dict):
            value = state.get("reason_summary")
            if isinstance(value, str):
                reason_summary = value
        return cls(
            id=record.id,
            flow_type=record.flow_type,
            status=record.status.value,
            current_step=record.current_step,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error_message=record.error_message,
            state=state,
            reason_summary=reason_summary,
            last_event_seq=last_event_seq,
            last_event_at=last_event_at,
            worker_health=(
                FlowWorkerHealthResponse.from_health(worker_health)
                if worker_health
                else None
            ),
        )


class FlowArtifactInfo(BaseModel):
    id: str
    kind: str
    path: str
    created_at: str
    metadata: Dict = Field(default_factory=dict)


def _build_flow_status_response(
    record: FlowRunRecord,
    repo_root: Path,
    *,
    store: Optional[FlowStore] = None,
) -> FlowStatusResponse:
    snapshot = build_flow_status_snapshot(repo_root, record, store)
    resp = FlowStatusResponse.from_record(
        record,
        last_event_seq=snapshot["last_event_seq"],
        last_event_at=snapshot["last_event_at"],
        worker_health=snapshot["worker_health"],
    )
    resp.ticket_progress = snapshot.get("ticket_progress")
    if snapshot.get("state") is not None:
        resp.state = snapshot["state"]
    return resp


def _start_flow_worker(
    repo_root: Path, run_id: str, state: FlowRoutesState
) -> Optional[subprocess.Popen]:
    normalized_run_id = _normalize_run_id(run_id)

    _reap_dead_worker(normalized_run_id, state)
    result = ensure_worker(repo_root, normalized_run_id)
    if result["status"] == "reused":
        health = result["health"]
        _logger.info(
            "Worker already active for run %s (pid=%s), skipping spawn",
            normalized_run_id,
            health.pid,
        )
        return None
    proc = result["proc"]
    stdout_handle = result["stdout"]
    stderr_handle = result["stderr"]
    with state.lock:
        state.active_workers[normalized_run_id] = (proc, stdout_handle, stderr_handle)
    _logger.info("Started flow worker for run %s (pid=%d)", normalized_run_id, proc.pid)
    return proc


def _stop_worker(run_id: str, state: FlowRoutesState, timeout: float = 10.0) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    with state.lock:
        handle = state.active_workers.get(normalized_run_id)
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

    _cleanup_worker_handle(normalized_run_id, state)


def build_flow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/flows", tags=["flows"])

    state = FlowRoutesState()

    def _ensure_state_in_app(request: Request) -> FlowRoutesState:
        if not hasattr(request.app.state, "flow_routes_state"):
            request.app.state.flow_routes_state = state
        return request.app.state.flow_routes_state

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

    def _get_diff_stats_by_dispatch_seq(
        repo_root: Path, *, run_id: str
    ) -> dict[int, dict[str, int]]:
        """Return mapping of dispatch_seq -> diff stats for the run."""
        store = _require_flow_store(repo_root)
        if store is None:
            return {}
        try:
            events = store.get_events_by_type(run_id, FlowEventType.DIFF_UPDATED)
        except Exception:
            events = []
        finally:
            try:
                store.close()
            except Exception:
                pass

        by_seq: dict[int, dict[str, int]] = {}
        for ev in events:
            data = ev.data or {}
            try:
                seq_val = int(data.get("dispatch_seq") or 0)
            except Exception:
                continue
            if seq_val <= 0:
                continue
            by_seq[seq_val] = {
                "insertions": int(data.get("insertions") or 0),
                "deletions": int(data.get("deletions") or 0),
                "files_changed": int(data.get("files_changed") or 0),
            }
        return by_seq

    @router.get("")
    async def list_flow_definitions(request: Request):
        state = _ensure_state_in_app(request)
        repo_root = find_repo_root()
        definitions = [
            _definition_info(_build_flow_definition(repo_root, flow_type, state))
            for flow_type in _supported_flow_types
        ]
        return {"definitions": definitions}

    @router.get("/runs", response_model=list[FlowStatusResponse])
    async def list_runs(
        request: Request, flow_type: Optional[str] = None, reconcile: bool = False
    ):
        _ensure_state_in_app(request)
        repo_root = find_repo_root()
        store = _require_flow_store(repo_root)
        records: list[FlowRunRecord] = []
        try:
            if store:
                records = store.list_flow_runs(flow_type=flow_type)
                if reconcile:
                    records = [
                        reconcile_flow_run(repo_root, rec, store, logger=_logger)[0]
                        for rec in records
                    ]
            else:
                records = _safe_list_flow_runs(
                    repo_root, flow_type=flow_type, recover_stuck=reconcile
                )
            return [
                _build_flow_status_response(rec, repo_root, store=store)
                for rec in records
            ]
        finally:
            if store:
                store.close()

    @router.get("/{flow_type}")
    async def get_flow_definition(request: Request, flow_type: str):
        state = _ensure_state_in_app(request)
        repo_root = find_repo_root()
        if flow_type not in _supported_flow_types:
            raise HTTPException(
                status_code=404, detail=f"Unknown flow type: {flow_type}"
            )
        definition = _build_flow_definition(repo_root, flow_type, state)
        return _definition_info(definition)

    async def _start_flow(
        flow_type: str,
        request: FlowStartRequest,
        state: FlowRoutesState,
        *,
        force_new: bool = False,
        validate_tickets: bool = True,
    ) -> FlowStatusResponse:
        if flow_type not in _supported_flow_types:
            raise HTTPException(
                status_code=404, detail=f"Unknown flow type: {flow_type}"
            )

        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root, flow_type, state)

        if flow_type == "ticket_flow" and validate_tickets:
            ticket_dir = repo_root / ".codex-autorunner" / "tickets"
            if force_new and not list_ticket_paths(ticket_dir):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No tickets found under .codex-autorunner/tickets. "
                        "Use /api/flows/ticket_flow/bootstrap to seed tickets."
                    ),
                )

            lint_errors = _validate_tickets(ticket_dir)
            if lint_errors:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Ticket validation failed",
                        "errors": lint_errors,
                    },
                )

        # Reuse an active/paused run unless force_new is requested.
        if not force_new:
            runs = _safe_list_flow_runs(
                repo_root, flow_type=flow_type, recover_stuck=True
            )
            active = _active_or_paused_run(runs)
            if active:
                _reap_dead_worker(active.id, state)
                _start_flow_worker(repo_root, active.id, state)
                store = _require_flow_store(repo_root)
                try:
                    response = _build_flow_status_response(
                        active, repo_root, store=store
                    )
                finally:
                    if store:
                        store.close()
                response.state = response.state or {}
                response.state["hint"] = "active_run_reused"
                return response

        run_id = _normalize_run_id(uuid.uuid4())

        record = await controller.start_flow(
            input_data=request.input_data,
            run_id=run_id,
            metadata=request.metadata,
        )

        _start_flow_worker(repo_root, run_id, state)

        store = _require_flow_store(repo_root)
        try:
            return _build_flow_status_response(record, repo_root, store=store)
        finally:
            if store:
                store.close()

    @router.post("/{flow_type}/start", response_model=FlowStatusResponse)
    async def start_flow(request: Request, flow_type: str, req: FlowStartRequest):
        state = _ensure_state_in_app(request)
        meta = req.metadata if isinstance(req.metadata, dict) else {}
        force_new = bool(meta.get("force_new"))
        return await _start_flow(flow_type, req, state, force_new=force_new)

    @router.get("/ticket_flow/bootstrap-check", response_model=BootstrapCheckResponse)
    async def bootstrap_check():
        """
        Determine whether ISSUE.md already exists and whether GitHub is available
        for fetching an issue before bootstrapping the ticket flow.
        """
        repo_root = find_repo_root()
        result = ux_bootstrap_check(repo_root, github_service_factory=GitHubService)
        if result.status == "ready":
            return BootstrapCheckResponse(status="ready")
        return BootstrapCheckResponse(
            status=result.status,
            github_available=result.github_available,
            repo=result.repo_slug,
        )

    @router.post("/ticket_flow/seed-issue")
    async def seed_issue(request: SeedIssueRequest):
        """Create .codex-autorunner/ISSUE.md from GitHub issue or user-provided text."""
        repo_root = find_repo_root()
        issue_path = issue_md_path(repo_root)
        issue_path.parent.mkdir(parents=True, exist_ok=True)

        # GitHub-backed path
        if request.issue_ref:
            try:
                seed = seed_issue_from_github(
                    repo_root, request.issue_ref, github_service_factory=GitHubService
                )
                atomic_write(issue_path, seed.content)
                return {
                    "status": "ok",
                    "source": "github",
                    "issue_number": seed.issue_number,
                    "repo": seed.repo_slug,
                }
            except GitHubError as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail=str(exc)
                ) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:  # pragma: no cover - defensive
                _logger.exception("Failed to seed ISSUE.md from GitHub: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Failed to fetch issue from GitHub"
                ) from exc

        # Manual text path
        if request.plan_text:
            content = seed_issue_from_text(request.plan_text)
            atomic_write(issue_path, content)
            return {"status": "ok", "source": "user_input"}

        raise HTTPException(
            status_code=400,
            detail="issue_ref or plan_text is required to seed ISSUE.md",
        )

    @router.post("/ticket_flow/bootstrap", response_model=FlowStatusResponse)
    async def bootstrap_ticket_flow(
        http_request: Request, request: Optional[FlowStartRequest] = None
    ):
        state = _ensure_state_in_app(http_request)
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        existing_tickets = list_ticket_paths(ticket_dir)
        tickets_exist = bool(existing_tickets)
        flow_request = request or FlowStartRequest()
        meta = flow_request.metadata if isinstance(flow_request.metadata, dict) else {}
        force_new = bool(meta.get("force_new"))

        if not force_new:
            records = _safe_list_flow_runs(
                repo_root, flow_type="ticket_flow", recover_stuck=True
            )
            active = _active_or_paused_run(records)
            if active:
                # Validate tickets before reusing active run
                lint_errors = _validate_tickets(ticket_dir)
                if lint_errors:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": "Ticket validation failed",
                            "errors": lint_errors,
                        },
                    )
                _reap_dead_worker(active.id, state)
                _start_flow_worker(repo_root, active.id, state)
                store = _require_flow_store(repo_root)
                try:
                    resp = _build_flow_status_response(active, repo_root, store=store)
                finally:
                    if store:
                        store.close()
                resp.state = resp.state or {}
                resp.state["hint"] = "active_run_reused"
                return resp

        seeded = False
        if not tickets_exist and not ticket_path.exists():
            template = """---
agent: codex
done: false
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing:
  - If GitHub is available, ask the user for the issue/PR URL or number and create `.codex-autorunner/ISSUE.md` from it.
  - If GitHub is not available, write `DISPATCH.md` with `mode: pause` asking the user to describe the work (or share a doc). After the reply, create `.codex-autorunner/ISSUE.md` with their input.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`:
  - `active_context.md` for current context and links
  - `decisions.md` for decisions/rationale
  - `spec.md` for requirements and constraints
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/dispatch/` if needed.
- Write `DISPATCH.md` to dispatch a message to the user:
  - Use `mode: pause` (handoff) to wait for user response. This pauses execution.
  - Use `mode: notify` (informational) to message the user but keep running.
"""
            ticket_path.write_text(template, encoding="utf-8")
            seeded = True

        meta = flow_request.metadata if isinstance(flow_request.metadata, dict) else {}
        payload = FlowStartRequest(
            input_data=flow_request.input_data,
            metadata=meta | {"seeded_ticket": seeded},
        )
        validate_tickets = not tickets_exist or force_new
        return await _start_flow(
            "ticket_flow",
            payload,
            state,
            force_new=force_new,
            validate_tickets=validate_tickets,
        )

    @router.get("/ticket_flow/tickets")
    async def list_ticket_files():
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        # Compute cumulative diff stats per ticket for the latest ticket_flow run.
        # This keeps ticket-level +/- counters visible after a run reaches COMPLETED.
        runs = _safe_list_flow_runs(
            repo_root, flow_type="ticket_flow", recover_stuck=True
        )
        latest_run = _latest_run(runs)
        diff_by_ticket: dict[str, dict[str, int]] = {}
        if latest_run:
            store = _require_flow_store(repo_root)
            if store is not None:
                try:
                    events = store.get_events_by_type(
                        latest_run.id, FlowEventType.DIFF_UPDATED
                    )
                except Exception:
                    events = []
                finally:
                    try:
                        store.close()
                    except Exception:
                        pass
                for ev in events:
                    data = ev.data or {}
                    ticket_id = data.get("ticket_id")
                    if not isinstance(ticket_id, str) or not ticket_id.strip():
                        continue
                    stats = diff_by_ticket.setdefault(
                        ticket_id,
                        {"insertions": 0, "deletions": 0, "files_changed": 0},
                    )
                    stats["insertions"] += int(data.get("insertions") or 0)
                    stats["deletions"] += int(data.get("deletions") or 0)
                    stats["files_changed"] += int(data.get("files_changed") or 0)

        tickets = []
        for path in list_ticket_paths(ticket_dir):
            doc, errors = read_ticket(path)
            idx = getattr(doc, "index", None) or parse_ticket_index(path.name)
            # When frontmatter is broken, still surface the raw ticket body so
            # the user can inspect and fix the file in the UI instead of seeing
            # an empty card.
            try:
                raw_body = path.read_text(encoding="utf-8")
                _, parsed_body = parse_markdown_frontmatter(raw_body)
            except Exception:
                parsed_body = None
            rel_path = safe_relpath(path, repo_root)
            tickets.append(
                {
                    "path": rel_path,
                    "index": idx,
                    "frontmatter": asdict(doc.frontmatter) if doc else None,
                    "body": doc.body if doc else parsed_body,
                    "errors": errors,
                    "diff_stats": diff_by_ticket.get(rel_path),
                }
            )
        return {
            "ticket_dir": safe_relpath(ticket_dir, repo_root),
            "tickets": tickets,
        }

    @router.get("/ticket_flow/tickets/{index}", response_model=TicketResponse)
    async def get_ticket(index: int):
        """Fetch a single ticket by index; return raw body even if frontmatter is invalid."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_path = _find_ticket_path_by_index(ticket_dir, index)

        if not ticket_path:
            raise HTTPException(status_code=404, detail=f"Ticket {index:03d} not found")

        doc, errors = read_ticket(ticket_path)
        if doc and not errors:
            return TicketResponse(
                path=safe_relpath(ticket_path, repo_root),
                index=doc.index,
                frontmatter=asdict(doc.frontmatter),
                body=doc.body,
            )

        # Mirror list endpoint: surface raw body for repair when frontmatter is broken.
        try:
            raw_body = ticket_path.read_text(encoding="utf-8")
            parsed_frontmatter, parsed_body = parse_markdown_frontmatter(raw_body)
        except Exception:
            parsed_frontmatter, parsed_body = {}, None

        return TicketResponse(
            path=safe_relpath(ticket_path, repo_root),
            index=parse_ticket_index(ticket_path.name),
            frontmatter=parsed_frontmatter or {},
            body=parsed_body,
        )

    @router.post("/ticket_flow/tickets", response_model=TicketResponse)
    async def create_ticket(request: TicketCreateRequest):
        """Create a new ticket with auto-generated index."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)

        # Find next available index
        existing_paths = list_ticket_paths(ticket_dir)
        existing_indices = set()
        for p in existing_paths:
            idx = parse_ticket_index(p.name)
            if idx is not None:
                existing_indices.add(idx)

        next_index = 1
        while next_index in existing_indices:
            next_index += 1

        # Build frontmatter (quote scalars to avoid YAML parse issues with colons, etc.)
        def _quote(val: Optional[str]) -> str:
            return (
                json.dumps(val) if val is not None else ""
            )  # JSON string is valid YAML scalar

        title_line = f"title: {_quote(request.title)}\n" if request.title else ""
        goal_line = f"goal: {_quote(request.goal)}\n" if request.goal else ""

        content = (
            "---\n"
            f"agent: {_quote(request.agent)}\n"
            "done: false\n"
            f"{title_line}"
            f"{goal_line}"
            "---\n\n"
            f"{request.body}\n"
        )

        ticket_path = ticket_dir / f"TICKET-{next_index:03d}.md"
        atomic_write(ticket_path, content)

        # Read back to validate and return
        doc, errors = read_ticket(ticket_path)
        if errors or not doc:
            raise HTTPException(
                status_code=400, detail=f"Failed to create valid ticket: {errors}"
            )

        return TicketResponse(
            path=safe_relpath(ticket_path, repo_root),
            index=doc.index,
            frontmatter=asdict(doc.frontmatter),
            body=doc.body,
        )

    @router.put("/ticket_flow/tickets/{index}", response_model=TicketResponse)
    async def update_ticket(index: int, request: TicketUpdateRequest):
        """Update an existing ticket by index."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_path = _find_ticket_path_by_index(ticket_dir, index)

        if not ticket_path:
            raise HTTPException(status_code=404, detail=f"Ticket {index:03d} not found")

        # Validate frontmatter before saving
        data, body = parse_markdown_frontmatter(request.content)
        _, errors = lint_ticket_frontmatter(data)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"message": "Invalid ticket frontmatter", "errors": errors},
            )

        atomic_write(ticket_path, request.content)

        # Read back to return validated data
        doc, read_errors = read_ticket(ticket_path)
        if read_errors or not doc:
            raise HTTPException(
                status_code=400, detail=f"Failed to save valid ticket: {read_errors}"
            )

        return TicketResponse(
            path=safe_relpath(ticket_path, repo_root),
            index=doc.index,
            frontmatter=asdict(doc.frontmatter),
            body=doc.body,
        )

    @router.delete("/ticket_flow/tickets/{index}", response_model=TicketDeleteResponse)
    async def delete_ticket(index: int):
        """Delete a ticket by index."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_path = _find_ticket_path_by_index(ticket_dir, index)

        if not ticket_path:
            raise HTTPException(status_code=404, detail=f"Ticket {index:03d} not found")

        rel_path = safe_relpath(ticket_path, repo_root)
        ticket_path.unlink()

        return TicketDeleteResponse(
            status="deleted",
            index=index,
            path=rel_path,
        )

    @router.post(
        "/ticket_flow/tickets/bulk-set-agent", response_model=TicketBulkUpdateResponse
    )
    async def bulk_set_agent_route(request: TicketBulkSetAgentRequest):
        """Bulk set agent for tickets, optionally limited by range."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        try:
            result = bulk_set_agent(
                ticket_dir,
                request.agent,
                request.range,
                repo_root=repo_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        lint_errors = _lint_after_ticket_update(ticket_dir)
        status = "ok" if not result.errors and not lint_errors else "error"
        return TicketBulkUpdateResponse(
            status=status,
            updated=result.updated,
            skipped=result.skipped,
            errors=result.errors,
            lint_errors=lint_errors,
        )

    @router.post(
        "/ticket_flow/tickets/bulk-clear-model", response_model=TicketBulkUpdateResponse
    )
    async def bulk_clear_model_route(request: TicketBulkClearModelRequest):
        """Bulk clear model/reasoning overrides for tickets, optionally limited by range."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        try:
            result = bulk_clear_model_pin(
                ticket_dir,
                request.range,
                repo_root=repo_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        lint_errors = _lint_after_ticket_update(ticket_dir)
        status = "ok" if not result.errors and not lint_errors else "error"
        return TicketBulkUpdateResponse(
            status=status,
            updated=result.updated,
            skipped=result.skipped,
            errors=result.errors,
            lint_errors=lint_errors,
        )

    @router.post("/{run_id}/stop", response_model=FlowStatusResponse)
    async def stop_flow(http_request: Request, run_id: uuid.UUID):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type, state)

        _stop_worker(run_id, state)

        updated = await controller.stop_flow(run_id)
        store = _require_flow_store(repo_root)
        try:
            return _build_flow_status_response(updated, repo_root, store=store)
        finally:
            if store:
                store.close()

    @router.post("/{run_id}/resume", response_model=FlowStatusResponse)
    async def resume_flow(http_request: Request, run_id: uuid.UUID):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type, state)

        # Validate tickets before resuming ticket_flow
        if record.flow_type == "ticket_flow":
            ticket_dir = repo_root / ".codex-autorunner" / "tickets"
            lint_errors = _validate_tickets(ticket_dir)
            if lint_errors:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Ticket validation failed",
                        "errors": lint_errors,
                    },
                )

        updated = await controller.resume_flow(run_id)
        _reap_dead_worker(run_id, state)
        _start_flow_worker(repo_root, run_id, state)

        store = _require_flow_store(repo_root)
        try:
            return _build_flow_status_response(updated, repo_root, store=store)
        finally:
            if store:
                store.close()

    @router.post("/{run_id}/reconcile", response_model=FlowStatusResponse)
    async def reconcile_flow(http_request: Request, run_id: uuid.UUID):
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        store = _require_flow_store(repo_root)
        if not store:
            raise HTTPException(status_code=503, detail="Flow store unavailable")
        try:
            record = reconcile_flow_run(repo_root, record, store, logger=_logger)[0]
            return _build_flow_status_response(record, repo_root, store=store)
        finally:
            store.close()

    @router.post("/{run_id}/archive")
    async def archive_flow(
        http_request: Request,
        run_id: uuid.UUID,
        delete_run: bool = True,
        force: bool = False,
    ):
        """Archive a completed flow by moving tickets to the run's artifact directory.

        Args:
            run_id: The flow run to archive.
            delete_run: Whether to delete the run record after archiving.
            force: If True, allow archiving flows stuck in stopping/paused state
                   by force-stopping the worker first.
        """
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)

        # Allow archiving terminal flows, or force-archiving stuck flows
        if not FlowRunStatus(record.status).is_terminal():
            if force and record.status in (
                FlowRunStatus.STOPPING,
                FlowRunStatus.PAUSED,
            ):
                # Force-stop any remaining worker before archiving
                _stop_worker(run_id, state, timeout=2.0)
                _logger.info(
                    "Force-archiving flow %s in %s state", run_id, record.status.value
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Can only archive completed/stopped/failed flows (use force=true for stuck flows)",
                )

        # Move tickets to run artifacts directory
        _, artifacts_root = _flow_paths(repo_root)
        archive_dir = artifacts_root / run_id / "archived_tickets"
        archive_dir.mkdir(parents=True, exist_ok=True)

        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        archived_count = 0
        for ticket_path in list_ticket_paths(ticket_dir):
            dest = archive_dir / ticket_path.name
            shutil.move(str(ticket_path), str(dest))
            archived_count += 1

        # Archive runs directory (dispatch_history, reply_history, etc.) to dismiss notifications
        outbox_paths = _resolve_outbox_for_record(record, repo_root)
        run_dir = outbox_paths.run_dir
        if run_dir.exists() and run_dir.is_dir():
            archived_runs_dir = artifacts_root / run_id / "archived_runs"
            shutil.move(str(run_dir), str(archived_runs_dir))

        # Delete run record if requested
        if delete_run:
            store = _require_flow_store(repo_root)
            if store:
                store.delete_flow_run(run_id)
                store.close()

        return {
            "status": "archived",
            "run_id": run_id,
            "tickets_archived": archived_count,
        }

    @router.get("/{run_id}/status", response_model=FlowStatusResponse)
    async def get_flow_status(
        http_request: Request, run_id: uuid.UUID, reconcile: bool = False
    ):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()

        _reap_dead_worker(run_id, state)

        record = _get_flow_record(repo_root, run_id)
        store = _require_flow_store(repo_root)
        try:
            if reconcile and store:
                record = reconcile_flow_run(repo_root, record, store, logger=_logger)[0]
            return _build_flow_status_response(record, repo_root, store=store)
        finally:
            if store:
                store.close()

    @router.get("/{run_id}/events")
    async def stream_flow_events(
        http_request: Request, run_id: uuid.UUID, after: Optional[int] = None
    ):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        controller = _get_flow_controller(repo_root, record.flow_type, state)

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

    @router.get("/{run_id}/dispatch_history")
    async def get_dispatch_history(http_request: Request, run_id: str):
        """Get dispatch history for a flow run.

        Returns all dispatches (agent->human communications) for this run.
        """
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        paths = _resolve_outbox_for_record(record, repo_root)

        # Pull diff stats from FlowStore keyed by dispatch sequence number so we
        # can enrich dispatch history entries without relying on DISPATCH.md metadata.
        diff_by_seq = _get_diff_stats_by_dispatch_seq(repo_root, run_id=normalized)

        history_entries = []
        history_dir = paths.dispatch_history_dir
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
                dispatch_dict = asdict(dispatch) if dispatch else None
                if dispatch_dict and dispatch:
                    dispatch_dict["is_handoff"] = dispatch.is_handoff
                    # Add structured diff stats (per turn summary), matched by seq.
                    try:
                        entry_seq_int = int(entry.name)
                    except Exception:
                        entry_seq_int = 0
                    if entry_seq_int and entry_seq_int in diff_by_seq:
                        dispatch_dict["diff_stats"] = diff_by_seq[entry_seq_int]
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
                history_entries.append(
                    {
                        "seq": entry.name,
                        "dispatch": dispatch_dict,
                        "errors": errors,
                        "attachments": attachments,
                        "path": safe_relpath(entry, repo_root),
                    }
                )

        return {"run_id": normalized, "history": history_entries}

    @router.get("/{run_id}/reply_history/{seq}/{file_path:path}")
    def get_reply_history_file(run_id: str, seq: str, file_path: str):
        repo_root = find_repo_root()
        db_path, _ = _flow_paths(repo_root)
        store = FlowStore(db_path)
        try:
            store.initialize()
            record = store.get_flow_run(run_id)
        finally:
            try:
                store.close()
            except Exception:
                pass
        if not record:
            raise HTTPException(status_code=404, detail="Run not found")

        if not (len(seq) == 4 and seq.isdigit()):
            raise HTTPException(status_code=400, detail="Invalid seq")

        try:
            filename = validate_single_filename(file_path)
        except SafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        input_data = dict(record.input_data or {})
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
        from ....tickets.replies import resolve_reply_paths

        reply_paths = resolve_reply_paths(
            workspace_root=workspace_root, runs_dir=runs_dir, run_id=run_id
        )
        target = reply_paths.reply_history_dir / seq / filename
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path=str(target), filename=filename)

    @router.get("/{run_id}/dispatch_history/{seq}/{file_path:path}")
    async def get_dispatch_file(run_id: str, seq: str, file_path: str):
        """Get an attachment file from a dispatch history entry."""
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        paths = _resolve_outbox_for_record(record, repo_root)

        base_history = paths.dispatch_history_dir.resolve()

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

    @router.get("/{run_id}/artifacts", response_model=list[FlowArtifactInfo])
    async def list_flow_artifacts(http_request: Request, run_id: str):
        state = _ensure_state_in_app(http_request)
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        controller = _get_flow_controller(repo_root, record.flow_type, state)

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
    async def get_flow_artifact(
        http_request: Request, run_id: str, kind: Optional[str] = None
    ):
        state = _ensure_state_in_app(http_request)
        normalized = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, normalized)
        controller = _get_flow_controller(repo_root, record.flow_type, state)

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
