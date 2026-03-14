import json
import logging
import re
import sqlite3
import subprocess
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ....core.config import load_repo_config
from ....core.config_contract import ConfigError
from ....core.file_chat_keys import (
    ticket_chat_scope,
    ticket_stable_id,
)
from ....core.flows import (
    FlowController,
    FlowDefinition,
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    archive_flow_run_artifacts,
    flow_run_duration_seconds,
)
from ....core.flows.reconciler import reconcile_flow_run
from ....core.flows.start_policy import (
    NO_TICKETS_START_ERROR,
    evaluate_ticket_start_policy,
)
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
from ....core.flows.worker_process import (
    FlowWorkerHealth,
    check_worker_health,
    write_worker_exit_info,
)
from ....core.orchestration import build_ticket_flow_orchestration_service
from ....core.runtime import RuntimeContext
from ....core.utils import atomic_write, find_repo_root
from ....flows.ticket_flow import build_ticket_flow_definition
from ....integrations.agents.build_agent_pool import build_agent_pool
from ....integrations.github.service import GitHubError, GitHubService
from ....tickets.bulk import bulk_clear_model_pin, bulk_set_agent
from ....tickets.files import (
    list_ticket_paths,
    parse_ticket_index,
    read_ticket,
    safe_relpath,
)
from ....tickets.frontmatter import parse_markdown_frontmatter
from ....tickets.lint import lint_ticket_frontmatter
from ..schemas import (
    TicketBulkClearModelRequest,
    TicketBulkSetAgentRequest,
    TicketBulkUpdateResponse,
    TicketCreateRequest,
    TicketDeleteResponse,
    TicketReorderRequest,
    TicketReorderResponse,
    TicketResponse,
    TicketUpdateRequest,
)
from ..services import flow_store as flow_store_service
from .flow_routes import FlowRoutesState
from .flow_routes.dependencies import build_default_flow_route_dependencies
from .flow_routes.history_artifacts import (
    get_diff_stats_by_dispatch_seq as extracted_get_diff_stats_by_dispatch_seq,
)
from .flow_routes.history_artifacts import (
    get_dispatch_history_file as extracted_get_dispatch_history_file,
)
from .flow_routes.history_artifacts import (
    get_reply_history as extracted_get_reply_history,
)
from .flow_routes.run_routes import (
    archive_flow_run as extracted_archive_flow_run,
)
from .flow_routes.run_routes import (
    get_flow_run_status as extracted_get_flow_run_status,
)
from .flow_routes.run_routes import (
    resume_flow_run as extracted_resume_flow_run,
)
from .flow_routes.run_routes import (
    stop_flow_worker as extracted_stop_flow_worker,
)
from .flow_routes.runtime_service import (
    list_orchestration_flow_run_records,
    resolve_flow_run_record,
)
from .flow_routes.ticket_bootstrap import (
    build_ticket_bootstrap_routes as extracted_build_ticket_bootstrap_routes,
)
from .flow_routes.ticket_bootstrap import run_bootstrap as extracted_run_bootstrap

_logger = logging.getLogger(__name__)

_supported_flow_types = ("ticket_flow",)
_FLOW_DB_CORRUPT_SUFFIX = ".corrupt"
_FLOW_DB_NOTICE_SUFFIX = ".corrupt.json"

# Keep the staged extraction seams visible while this module remains the composition owner.
_EXTRACTED_FLOW_ROUTE_SEAMS = (
    extracted_get_diff_stats_by_dispatch_seq,
    extracted_get_dispatch_history_file,
    extracted_get_reply_history,
    extracted_stop_flow_worker,
    extracted_resume_flow_run,
    extracted_archive_flow_run,
    extracted_get_flow_run_status,
    extracted_build_ticket_bootstrap_routes,
    extracted_run_bootstrap,
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_probably_corrupt_flow_db_error(exc: Exception, db_path: Path) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    if "file is not a database" in msg or "database disk image is malformed" in msg:
        return True
    if "disk i/o error" in msg:
        try:
            header = db_path.read_bytes()[:16]
        except Exception:
            return False
        return header not in (b"", b"SQLite format 3\x00")
    return False


def _rotate_corrupt_flow_db(db_path: Path, detail: str) -> Optional[Path]:
    stamp = _utc_stamp()
    backup_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_CORRUPT_SUFFIX}.{stamp}")
    notice_path = db_path.with_name(f"{db_path.name}{_FLOW_DB_NOTICE_SUFFIX}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_value: str = ""
    if db_path.exists():
        try:
            db_path.replace(backup_path)
            backup_value = str(backup_path)
        except Exception:
            backup_value = ""

    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(f"{db_path.name}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass

    notice = {
        "status": "corrupt",
        "message": "Flow store reset due to corrupted flows.db.",
        "detail": detail,
        "detected_at": stamp,
        "backup_path": backup_value,
    }
    try:
        atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
    except Exception:
        _logger.warning("Failed to write flow DB corruption notice at %s", notice_path)
    return backup_path if backup_value else None


def _evict_cached_controller(
    repo_root: Path, flow_type: str, state: FlowRoutesState
) -> None:
    key = (repo_root.resolve(), flow_type)
    with state.lock:
        controller = state.controller_cache.pop(key, None)
    if not controller:
        return
    try:
        controller.shutdown()
    except Exception:
        pass


def _recover_flow_store_if_possible(
    repo_root: Path,
    flow_type: str,
    state: FlowRoutesState,
    exc: Exception,
) -> bool:
    db_path, _ = _flow_paths(repo_root)
    if not _is_probably_corrupt_flow_db_error(exc, db_path):
        return False

    backup_path = _rotate_corrupt_flow_db(db_path, str(exc))
    _evict_cached_controller(repo_root, flow_type, state)
    store = FlowStore(db_path)
    try:
        store.initialize()
        _logger.warning(
            "Recovered corrupted flow DB at %s (backup=%s, reason=%s)",
            db_path,
            str(backup_path) if backup_path else "unavailable",
            exc,
        )
        return True
    except Exception as recover_exc:
        _logger.warning(
            "Flow DB recovery failed at %s after error %s: %s",
            db_path,
            exc,
            recover_exc,
        )
        return False
    finally:
        try:
            store.close()
        except Exception:
            pass


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    return flow_store_service.flow_paths(repo_root)


def _ticket_dir(repo_root: Path) -> Path:
    repo_root = repo_root.resolve()
    return repo_root / ".codex-autorunner" / "tickets"


def _find_ticket_path_by_index(ticket_dir: Path, index: int) -> Optional[Path]:
    for path in list_ticket_paths(ticket_dir):
        idx = parse_ticket_index(path.name)
        if idx == index:
            return path
    return None


_TICKET_NAME_RE = re.compile(r"^TICKET-(\d+)([^/]*)\.md$", re.IGNORECASE)


def _ticket_suffix(path: Path) -> str:
    match = _TICKET_NAME_RE.match(path.name)
    if not match:
        return ""
    return match.group(2) or ""


def _planned_ticket_renames(order: list[Path]) -> list[tuple[Path, Path]]:
    if not order:
        return []
    existing_width = max(
        3,
        max(
            (
                len(m.group(1))
                for m in (_TICKET_NAME_RE.match(path.name) for path in order)
                if m is not None
            ),
            default=3,
        ),
    )
    planned: list[tuple[Path, Path]] = []
    for new_idx, original_path in enumerate(order, start=1):
        suffix = _ticket_suffix(original_path)
        target_name = f"TICKET-{new_idx:0{existing_width}d}{suffix}.md"
        target_path = original_path.with_name(target_name)
        planned.append((original_path, target_path))
    return planned


def _rename_ticket_order(order: list[Path]) -> None:
    planned = _planned_ticket_renames(order)
    if not planned:
        return
    staged: list[tuple[Path, Path]] = []
    for original_path, _target_path in planned:
        tmp = original_path.with_name(
            f".tmp-reorder-{uuid.uuid4().hex}-{original_path.name}"
        )
        original_path.rename(tmp)
        staged.append((tmp, _target_path))
    for tmp_path, target_path in staged:
        tmp_path.rename(target_path)


def _sync_active_run_current_ticket_paths_after_reorder(
    repo_root: Path, renamed_paths: list[tuple[Path, Path]]
) -> None:
    flow_store_service.sync_active_run_current_ticket_paths_after_reorder(
        repo_root,
        renamed_paths,
        require_store=_require_flow_store,
        active_or_paused_run_selector=_active_or_paused_run,
        logger=_logger,
    )


def _require_flow_store(repo_root: Path) -> Optional[FlowStore]:
    return flow_store_service.require_flow_store(repo_root, logger=_logger)


def _safe_list_flow_runs(
    repo_root: Path, flow_type: Optional[str] = None, *, recover_stuck: bool = False
) -> list[FlowRunRecord]:
    return flow_store_service.safe_list_flow_runs(
        repo_root,
        flow_type=flow_type,
        recover_stuck=recover_stuck,
        logger=_logger,
    )


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
        agent_pool = build_agent_pool(engine.config)
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
        cached = state.controller_cache.get(key)
    if cached is not None:
        try:
            cached.initialize()
            return cached
        except Exception as exc:
            if not _recover_flow_store_if_possible(repo_root, flow_type, state, exc):
                _evict_cached_controller(repo_root, flow_type, state)
                _logger.warning("Failed to initialize cached flow controller: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="Flows unavailable; initialize the repo first.",
                ) from exc

    db_path, artifacts_root = _flow_paths(repo_root)
    definition = _build_flow_definition(repo_root, flow_type, state)

    def _new_controller() -> FlowController:
        return FlowController(
            definition=definition,
            db_path=db_path,
            artifacts_root=artifacts_root,
        )

    controller = _new_controller()
    try:
        controller.initialize()
    except Exception as exc:
        if _recover_flow_store_if_possible(repo_root, flow_type, state, exc):
            controller = _new_controller()
            try:
                controller.initialize()
            except Exception as retry_exc:
                _logger.warning(
                    "Failed to initialize flow controller after recovery: %s", retry_exc
                )
                raise HTTPException(
                    status_code=503,
                    detail="Flows unavailable; initialize the repo first.",
                ) from retry_exc
        else:
            _logger.warning("Failed to initialize flow controller: %s", exc)
            raise HTTPException(
                status_code=503, detail="Flows unavailable; initialize the repo first."
            ) from exc
    with state.lock:
        state.controller_cache[key] = controller
    return controller


def _get_flow_record(repo_root: Path, run_id: str) -> FlowRunRecord:
    return flow_store_service.get_flow_record(
        repo_root, run_id, require_store=_require_flow_store
    )


def _active_or_paused_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    for record in records:
        if record.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
            return record
    return None


def _coerce_ticket_diff_ref(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _ticket_diff_event_ref(data: dict[str, object]) -> Optional[str]:
    for key in ("ticket_key", "ticket_path", "ticket_id"):
        ref = _coerce_ticket_diff_ref(data.get(key))
        if ref:
            return ref
    return None


def _merge_ticket_diff_stats(
    refs: list[str], diff_by_ref: dict[str, dict[str, int]]
) -> Optional[dict[str, int]]:
    merged = {"insertions": 0, "deletions": 0, "files_changed": 0}
    matched = False
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        stats = diff_by_ref.get(ref)
        if not isinstance(stats, dict):
            continue
        matched = True
        merged["insertions"] += int(stats.get("insertions") or 0)
        merged["deletions"] += int(stats.get("deletions") or 0)
        merged["files_changed"] += int(stats.get("files_changed") or 0)
    return merged if matched else None


def _normalize_run_id(run_id: Union[str, uuid.UUID]) -> str:
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid run_id") from None


def _validate_tickets(ticket_dir: Path) -> list[str]:
    """Validate all tickets in the directory and return linting/frontmatter errors."""
    return list(evaluate_ticket_start_policy(ticket_dir).lint_errors)


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
    if proc and proc.poll() is not None:
        try:
            write_worker_exit_info(
                find_repo_root(), run_id, returncode=getattr(proc, "returncode", None)
            )
        except Exception:
            pass
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
    exit_code: Optional[int] = None
    stderr_tail: Optional[str] = None

    @classmethod
    def from_health(cls, health: FlowWorkerHealth) -> "FlowWorkerHealthResponse":
        return cls(
            status=health.status,
            pid=health.pid,
            is_alive=health.is_alive,
            message=health.message,
            exit_code=getattr(health, "exit_code", None),
            stderr_tail=getattr(health, "stderr_tail", None),
        )


class FlowStatusResponse(BaseModel):
    id: str
    flow_type: str
    status: str
    current_step: Optional[str]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    duration_seconds: Optional[float] = None
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
            duration_seconds=flow_run_duration_seconds(record),
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


def _build_flow_orchestration_service(repo_root: Path, flow_type: str):
    if flow_type != "ticket_flow":
        raise HTTPException(status_code=404, detail=f"Unknown flow type: {flow_type}")
    return build_ticket_flow_orchestration_service(workspace_root=repo_root)


async def _start_flow_via_controller(
    repo_root: Path,
    flow_type: str,
    request: FlowStartRequest,
    state: FlowRoutesState,
    *,
    run_id: str,
) -> FlowRunRecord:
    controller = _get_flow_controller(repo_root, flow_type, state)
    try:
        return await controller.start_flow(
            input_data=request.input_data,
            run_id=run_id,
            metadata=request.metadata,
        )
    except Exception as exc:
        if _recover_flow_store_if_possible(repo_root, flow_type, state, exc):
            controller = _get_flow_controller(repo_root, flow_type, state)
            retry_run_id = _normalize_run_id(uuid.uuid4())
            try:
                return await controller.start_flow(
                    input_data=request.input_data,
                    run_id=retry_run_id,
                    metadata=request.metadata,
                )
            except sqlite3.Error as retry_exc:
                raise HTTPException(
                    status_code=503, detail="Flows database unavailable"
                ) from retry_exc
        if isinstance(exc, sqlite3.Error):
            raise HTTPException(
                status_code=503, detail="Flows database unavailable"
            ) from exc
        raise


def _start_flow_worker(
    repo_root: Path, run_id: str, state: FlowRoutesState
) -> Optional[subprocess.Popen]:
    normalized_run_id = _normalize_run_id(run_id)

    _reap_dead_worker(normalized_run_id, state)
    result: dict = ensure_worker(repo_root, normalized_run_id)
    if result["status"] == "reused":
        health = result["health"]
        _logger.info(
            "Worker already active for run %s (pid=%s), skipping spawn",
            normalized_run_id,
            health.pid,
        )
        return None
    proc: subprocess.Popen = result["proc"]
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
        from typing import cast

        if not hasattr(request.app.state, "flow_routes_state"):
            request.app.state.flow_routes_state = state
        return cast(FlowRoutesState, request.app.state.flow_routes_state)

    deps = build_default_flow_route_dependencies()
    deps.find_repo_root = find_repo_root
    deps.build_flow_orchestration_service = _build_flow_orchestration_service
    deps.require_flow_store = _require_flow_store
    deps.safe_list_flow_runs = _safe_list_flow_runs
    deps.build_flow_status_response = _build_flow_status_response
    deps.get_flow_record = _get_flow_record
    deps.get_flow_controller = _get_flow_controller
    deps.start_flow_worker = _start_flow_worker
    deps.recover_flow_store_if_possible = _recover_flow_store_if_possible
    deps.bootstrap_check = ux_bootstrap_check

    from .flow_routes.status_history_routes import (
        build_status_history_routes,
    )

    status_history_router, _ = build_status_history_routes(deps, prefix="")
    router.include_router(status_history_router)

    def _definition_info(definition: FlowDefinition) -> Dict:
        return {
            "type": definition.flow_type,
            "name": definition.name,
            "description": definition.description,
            "input_schema": definition.input_schema or {},
        }

    @router.get("")
    async def list_flow_definitions(request: Request):
        state = _ensure_state_in_app(request)
        repo_root = find_repo_root()
        definitions = [
            _definition_info(_build_flow_definition(repo_root, flow_type, state))
            for flow_type in _supported_flow_types
        ]
        return {"definitions": definitions}

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

        if flow_type == "ticket_flow" and validate_tickets:
            ticket_dir = repo_root / ".codex-autorunner" / "tickets"
            ticket_policy = evaluate_ticket_start_policy(ticket_dir)
            lint_errors = list(ticket_policy.lint_errors)
            if lint_errors:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Ticket validation failed",
                        "errors": lint_errors,
                    },
                )
            if not ticket_policy.has_tickets:
                raise HTTPException(status_code=400, detail=NO_TICKETS_START_ERROR)

        # Reuse an active/paused run unless force_new is requested.
        if not force_new:
            try:
                active_records = list_orchestration_flow_run_records(
                    repo_root,
                    flow_type=flow_type,
                    flow_target_id=flow_type,
                    active_only=True,
                    build_service=_build_flow_orchestration_service,
                )
            except Exception:
                active_records = []
            active = active_records[0] if active_records else None
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
        try:
            service = _build_flow_orchestration_service(repo_root, flow_type)
            record = resolve_flow_run_record(
                repo_root,
                await service.start_flow_run(
                    flow_type,
                    input_data=request.input_data,
                    run_id=run_id,
                    metadata=request.metadata,
                ),
            )
        except ConfigError:
            record = await _start_flow_via_controller(
                repo_root, flow_type, request, state, run_id=run_id
            )
        except Exception as exc:
            if _recover_flow_store_if_possible(repo_root, flow_type, state, exc):
                run_id = _normalize_run_id(uuid.uuid4())
                try:
                    service = _build_flow_orchestration_service(repo_root, flow_type)
                    record = resolve_flow_run_record(
                        repo_root,
                        await service.start_flow_run(
                            flow_type,
                            input_data=request.input_data,
                            run_id=run_id,
                            metadata=request.metadata,
                        ),
                    )
                except ConfigError:
                    record = await _start_flow_via_controller(
                        repo_root, flow_type, request, state, run_id=run_id
                    )
                except sqlite3.Error as retry_exc:
                    raise HTTPException(
                        status_code=503, detail="Flows database unavailable"
                    ) from retry_exc
            elif isinstance(exc, sqlite3.Error):
                raise HTTPException(
                    status_code=503, detail="Flows database unavailable"
                ) from exc
            else:
                raise

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
                    repo_root,
                    request.issue_ref,
                    github_service_factory=GitHubService,  # type: ignore[arg-type]
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
            bootstrap_ticket_id = f"tkt_{uuid.uuid4().hex}"
            template = f"""---
agent: codex
done: false
ticket_id: "{bootstrap_ticket_id}"
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
        # Compute cumulative diff stats per ticket across ticket_flow runs.
        # Use stable ticket identity where available so restarts and reorders do
        # not drop line-change history for the same logical ticket.
        runs = _safe_list_flow_runs(
            repo_root, flow_type="ticket_flow", recover_stuck=True
        )
        diff_by_ref: dict[str, dict[str, int]] = {}
        if runs:
            store = _require_flow_store(repo_root)
            if store is not None:
                try:
                    for run in runs:
                        try:
                            events = store.get_events_by_type(
                                run.id, FlowEventType.DIFF_UPDATED
                            )
                        except Exception:
                            continue
                        for ev in events:
                            data = ev.data or {}
                            ref = _ticket_diff_event_ref(data)
                            if not ref:
                                continue
                            stats = diff_by_ref.setdefault(
                                ref,
                                {
                                    "insertions": 0,
                                    "deletions": 0,
                                    "files_changed": 0,
                                },
                            )
                            stats["insertions"] += int(data.get("insertions") or 0)
                            stats["deletions"] += int(data.get("deletions") or 0)
                            stats["files_changed"] += int(
                                data.get("files_changed") or 0
                            )
                finally:
                    try:
                        store.close()
                    except Exception:
                        pass

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
            stable_ticket_id = ticket_stable_id(path)
            diff_refs = [stable_ticket_id] if stable_ticket_id else [rel_path]
            tickets.append(
                {
                    "path": rel_path,
                    "index": idx,
                    "chat_key": ticket_chat_scope(idx, path) if idx else None,
                    "frontmatter": asdict(doc.frontmatter) if doc else None,
                    "body": doc.body if doc else parsed_body,
                    "errors": errors,
                    "diff_stats": _merge_ticket_diff_stats(diff_refs, diff_by_ref),
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
                chat_key=ticket_chat_scope(doc.index, ticket_path),
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
            index=parse_ticket_index(ticket_path.name) or 0,
            chat_key=ticket_chat_scope(index, ticket_path),
            frontmatter=parsed_frontmatter or {},
            body=parsed_body or "",
        )

    @router.post("/ticket_flow/tickets", response_model=TicketResponse)
    async def create_ticket(request: TicketCreateRequest):
        """Create a new ticket with auto-generated index."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)

        # Find next available index
        existing_paths = list_ticket_paths(ticket_dir)
        existing_indices = []
        for p in existing_paths:
            idx = parse_ticket_index(p.name)
            if idx is not None:
                existing_indices.append(idx)

        # Always append at the end of the sequence; do not backfill gaps.
        next_index = (max(existing_indices) + 1) if existing_indices else 1

        # Build frontmatter (quote scalars to avoid YAML parse issues with colons, etc.)
        def _quote(val: Optional[str]) -> str:
            return (
                json.dumps(val) if val is not None else ""
            )  # JSON string is valid YAML scalar

        title_line = f"title: {_quote(request.title)}\n" if request.title else ""
        goal_line = f"goal: {_quote(request.goal)}\n" if request.goal else ""
        ticket_id = f"tkt_{uuid.uuid4().hex}"
        ticket_id_line = f"ticket_id: {_quote(ticket_id)}\n"

        content = (
            "---\n"
            f"agent: {_quote(request.agent)}\n"
            "done: false\n"
            f"{ticket_id_line}"
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
            chat_key=ticket_chat_scope(doc.index, ticket_path),
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
            chat_key=ticket_chat_scope(doc.index, ticket_path),
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

    @router.post("/ticket_flow/tickets/reorder", response_model=TicketReorderResponse)
    async def reorder_ticket(request: TicketReorderRequest):
        """Reorder one ticket relative to another ticket."""
        repo_root = find_repo_root()
        ticket_dir = repo_root / ".codex-autorunner" / "tickets"
        ticket_paths = list_ticket_paths(ticket_dir)
        indices = [parse_ticket_index(path.name) for path in ticket_paths]
        ordered_indices = [idx for idx in indices if idx is not None]

        source_index = request.source_index
        destination_index = request.destination_index
        place_after = bool(request.place_after)

        if source_index == destination_index:
            lint_errors = _lint_after_ticket_update(ticket_dir)
            return TicketReorderResponse(
                status="ok" if not lint_errors else "error",
                source_index=source_index,
                destination_index=destination_index,
                place_after=place_after,
                lint_errors=lint_errors,
            )
        if source_index not in ordered_indices:
            raise HTTPException(
                status_code=404, detail=f"Ticket {source_index:03d} not found"
            )
        if destination_index not in ordered_indices:
            raise HTTPException(
                status_code=404, detail=f"Ticket {destination_index:03d} not found"
            )

        source_pos = ordered_indices.index(source_index)
        destination_pos = ordered_indices.index(destination_index)
        desired_pos = destination_pos + (1 if place_after else 0)
        if source_pos < desired_pos:
            desired_pos -= 1
        desired_pos = max(0, min(desired_pos, len(ticket_paths) - 1))

        if desired_pos != source_pos:
            source_path = ticket_paths[source_pos]
            remaining_paths = [
                path for idx, path in enumerate(ticket_paths) if idx != source_pos
            ]
            reordered_paths = (
                remaining_paths[:desired_pos]
                + [source_path]
                + remaining_paths[desired_pos:]
            )
            renamed_paths = _planned_ticket_renames(reordered_paths)
            try:
                _rename_ticket_order(reordered_paths)
                _sync_active_run_current_ticket_paths_after_reorder(
                    repo_root, renamed_paths
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Failed to reorder ticket: {exc}"
                ) from exc

        lint_errors = _lint_after_ticket_update(ticket_dir)
        return TicketReorderResponse(
            status="ok" if not lint_errors else "error",
            source_index=source_index,
            destination_index=destination_index,
            place_after=place_after,
            lint_errors=lint_errors,
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
    async def stop_flow(http_request: Request, run_id: str):
        state = _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        service = _build_flow_orchestration_service(repo_root, record.flow_type)

        _stop_worker(run_id, state)

        updated = await service.stop_flow_run(run_id)
        store = _require_flow_store(repo_root)
        try:
            return _build_flow_status_response(
                resolve_flow_run_record(repo_root, updated, store=store),
                repo_root,
                store=store,
            )
        finally:
            if store:
                store.close()

    @router.post("/{run_id}/resume", response_model=FlowStatusResponse)
    async def resume_flow(http_request: Request, run_id: str, force: bool = False):
        _ensure_state_in_app(http_request)
        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        record = _get_flow_record(repo_root, run_id)
        service = _build_flow_orchestration_service(repo_root, record.flow_type)

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

        try:
            updated = await service.resume_flow_run(run_id, force=force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        store = _require_flow_store(repo_root)
        try:
            return _build_flow_status_response(
                resolve_flow_run_record(repo_root, updated, store=store),
                repo_root,
                store=store,
            )
        finally:
            if store:
                store.close()

    @router.post("/{run_id}/reconcile", response_model=FlowStatusResponse)
    async def reconcile_flow(http_request: Request, run_id: str):
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
        run_id: str,
        delete_run: bool = True,
        force: bool = False,
    ):
        """Archive a completed flow and reset live ticket/contextspace state.

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

        try:
            summary = archive_flow_run_artifacts(
                repo_root,
                run_id=run_id,
                force=force,
                delete_run=delete_run,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "status": "archived",
            "run_id": run_id,
            "tickets_archived": summary["archived_tickets"],
            "archived_runs": summary["archived_runs"],
            "archived_contextspace": summary["archived_contextspace"],
            "missing_paths": summary.get("missing_paths", []),
        }

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
