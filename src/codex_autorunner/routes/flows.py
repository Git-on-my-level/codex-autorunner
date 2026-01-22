import json
import logging
import subprocess
import sys
import uuid
from pathlib import Path
from typing import IO, Dict, Optional, Tuple, Union

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core.flows import (
    FlowController,
    FlowRunRecord,
)
from ..flows.pr_flow import build_pr_flow_definition

_logger = logging.getLogger(__name__)

_active_workers: Dict[
    str, Tuple[subprocess.Popen, Optional[IO[bytes]], Optional[IO[bytes]]]
] = {}
_controller_cache: Dict[Path, FlowController] = {}


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
    if proc.poll() is None:
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
    if proc.poll() is not None:
        _cleanup_worker_handle(run_id)


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


def _get_flow_controller(repo_root: Path) -> FlowController:
    repo_root = repo_root.resolve()
    if repo_root in _controller_cache:
        return _controller_cache[repo_root]

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"

    controller = FlowController(
        definition=build_pr_flow_definition(),
        db_path=db_path,
        artifacts_root=artifacts_root,
    )
    controller.initialize()
    _controller_cache[repo_root] = controller
    return controller


def _start_flow_worker(repo_root: Path, run_id: str) -> subprocess.Popen:
    normalized_run_id = _normalize_run_id(run_id)
    _reap_dead_worker(normalized_run_id)

    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / normalized_run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = artifacts_dir / "worker.out.log"
    stderr_path = artifacts_dir / "worker.err.log"

    stdout_handle = open(stdout_path, "ab")
    stderr_handle = open(stderr_path, "ab")

    cmd = [
        sys.executable,
        "-m",
        "codex_autorunner",
        "flow",
        "worker",
        "--run-id",
        normalized_run_id,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=stdout_handle,
        stderr=stderr_handle,
    )
    _active_workers[normalized_run_id] = (proc, stdout_handle, stderr_handle)
    _logger.info("Started flow worker for run %s (pid=%d)", normalized_run_id, proc.pid)
    return proc


def _stop_worker(run_id: str, timeout: float = 10.0) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    handle = _active_workers.get(normalized_run_id)
    if not handle:
        return

    proc, *_ = handle
    if proc.poll() is None:
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

    @router.get("")
    async def list_flow_definitions():
        return {
            "definitions": [
                {
                    "type": "pr_flow",
                    "description": "Pull request automation flow",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "issue_url": {"type": "string"},
                            "pr_url": {"type": "string"},
                            "branch": {"type": "string"},
                        },
                    },
                }
            ]
        }

    @router.post("/pr_flow/start", response_model=FlowStatusResponse)
    async def start_pr_flow(request: FlowStartRequest):
        from ..core.utils import find_repo_root

        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        run_id = _normalize_run_id(uuid.uuid4())

        try:
            record = await controller.start_flow(
                input_data=request.input_data,
                run_id=run_id,
                metadata=request.metadata,
            )

            if run_id in _active_workers:
                _logger.info("Worker already active for run %s, skipping spawn", run_id)
            else:
                _start_flow_worker(repo_root, run_id)

            return FlowStatusResponse.from_record(record)
        except Exception as e:
            _logger.exception("Failed to start PR flow: %s", e)
            raise

    @router.post("/{run_id}/stop", response_model=FlowStatusResponse)
    async def stop_flow(run_id: uuid.UUID):
        from ..core.utils import find_repo_root

        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        _stop_worker(run_id)

        record = await controller.stop_flow(run_id)
        return FlowStatusResponse.from_record(record)

    @router.post("/{run_id}/resume", response_model=FlowStatusResponse)
    async def resume_flow(run_id: uuid.UUID):
        from ..core.utils import find_repo_root

        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        record = await controller.resume_flow(run_id)
        _reap_dead_worker(run_id)
        if run_id in _active_workers:
            _logger.info("Worker already active for run %s, skipping spawn", run_id)
        else:
            _start_flow_worker(repo_root, run_id)

        return FlowStatusResponse.from_record(record)

    @router.get("/{run_id}/status", response_model=FlowStatusResponse)
    async def get_flow_status(run_id: uuid.UUID):
        from ..core.utils import find_repo_root

        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        _reap_dead_worker(run_id)

        record = controller.get_status(run_id)
        if not record:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")

        return FlowStatusResponse.from_record(record)

    @router.get("/{run_id}/events")
    async def stream_flow_events(run_id: uuid.UUID, after: Optional[int] = None):
        from ..core.utils import find_repo_root

        run_id = _normalize_run_id(run_id)
        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        record = controller.get_status(run_id)
        if not record:
            raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")

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

    @router.get("/{run_id}/artifacts", response_model=list[FlowArtifactInfo])
    async def list_flow_artifacts(run_id: str):
        from ..core.utils import find_repo_root

        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        record = controller.get_status(run_id)
        if not record:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")

        artifacts = controller.get_artifacts(run_id)
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
        from ..core.utils import find_repo_root

        repo_root = find_repo_root()
        controller = _get_flow_controller(repo_root)

        record = controller.get_status(run_id)
        if not record:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")

        artifacts_root = controller.get_artifacts_dir(run_id)
        if not artifacts_root:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=404, detail=f"Artifact directory not found for run {run_id}"
            )

        artifacts = controller.get_artifacts(run_id)

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
