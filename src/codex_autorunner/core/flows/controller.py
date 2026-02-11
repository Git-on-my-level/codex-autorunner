import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Set

from ...manifest import ManifestError, load_manifest
from ..git_utils import run_git
from ..lifecycle_events import LifecycleEventEmitter
from ..utils import find_repo_root
from .definition import FlowDefinition
from .models import FlowEvent, FlowRunRecord, FlowRunStatus
from .runtime import FlowRuntime
from .store import FlowStore


def _find_hub_root(repo_root: Optional[Path] = None) -> Optional[Path]:
    if repo_root is None:
        repo_root = find_repo_root()
    if repo_root is None:
        return None
    current = repo_root
    for _ in range(5):
        manifest_path = current / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


_logger = logging.getLogger(__name__)
_RESUME_SIGNAL_REQUIRED_REASON_CODES = {"needs_user_fix", "infra_error", "loop_no_diff"}


class FlowController:
    def __init__(
        self,
        definition: FlowDefinition,
        db_path: Path,
        artifacts_root: Path,
        durable: bool = False,
        hub_root: Optional[Path] = None,
    ):
        self.definition = definition
        self.db_path = db_path
        self.artifacts_root = artifacts_root
        self.store = FlowStore(db_path, durable=durable)
        self._event_listeners: Set[Callable[[FlowEvent], None]] = set()
        self._lifecycle_event_listeners: Set[
            Callable[[str, str, str, dict, str], None]
        ] = set()
        self._lock = asyncio.Lock()
        self._lifecycle_emitter: Optional[LifecycleEventEmitter] = None
        self._repo_id = ""
        if hub_root is None:
            hub_root = _find_hub_root(db_path.parent.parent if db_path else None)
        if hub_root is not None:
            self._lifecycle_emitter = LifecycleEventEmitter(hub_root)
            self.add_lifecycle_event_listener(self._emit_to_lifecycle_store)
            self._repo_id = self._resolve_repo_id(hub_root)

    def _resolve_repo_id(self, hub_root: Path) -> str:
        repo_root = self.db_path.parent.parent if self.db_path else None
        if repo_root is None:
            return ""
        manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
        try:
            manifest = load_manifest(manifest_path, hub_root)
        except ManifestError:
            return ""
        entry = manifest.get_by_path(hub_root, repo_root)
        return entry.id if entry else ""

    def initialize(self) -> None:
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store.initialize()

    def shutdown(self) -> None:
        self.store.close()

    async def start_flow(
        self,
        input_data: Dict[str, Any],
        run_id: Optional[str] = None,
        initial_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FlowRunRecord:
        """Create a new flow run record without executing the flow."""
        if run_id is None:
            run_id = str(uuid.uuid4())

        async with self._lock:
            existing = self.store.get_flow_run(run_id)
            if existing:
                raise ValueError(f"Flow run {run_id} already exists")

            self._prepare_artifacts_dir(run_id)

            record = self.store.create_flow_run(
                run_id=run_id,
                flow_type=self.definition.flow_type,
                input_data=input_data,
                metadata=metadata,
                state=initial_state or {},
                current_step=self.definition.initial_step,
            )

            return record

    async def run_flow(
        self, run_id: str, initial_state: Optional[Dict[str, Any]] = None
    ) -> FlowRunRecord:
        """Run or resume a flow to completion in-process (used by workers/tests)."""
        runtime = FlowRuntime(
            definition=self.definition,
            store=self.store,
            emit_event=self._emit_event,
            emit_lifecycle_event=self._emit_lifecycle,
        )
        return await runtime.run_flow(run_id=run_id, initial_state=initial_state)

    async def stop_flow(self, run_id: str) -> FlowRunRecord:
        record = self.store.set_stop_requested(run_id, True)
        if not record:
            raise ValueError(f"Flow run {run_id} not found")

        if record.status == FlowRunStatus.RUNNING:
            updated = self.store.update_flow_run_status(
                run_id=run_id,
                status=FlowRunStatus.STOPPING,
            )
            if updated:
                record = updated

        updated = self.store.get_flow_run(run_id)
        if not updated:
            raise RuntimeError(f"Failed to get record for run {run_id}")
        return updated

    async def resume_flow(self, run_id: str, *, force: bool = False) -> FlowRunRecord:
        async with self._lock:
            record = self.store.get_flow_run(run_id)
            if not record:
                raise ValueError(f"Flow run {run_id} not found")

            if record.status == FlowRunStatus.RUNNING:
                raise ValueError(f"Flow run {run_id} is already active")

            cleared = self.store.set_stop_requested(run_id, False)
            if not cleared:
                raise RuntimeError(f"Failed to clear stop flag for run {run_id}")
            if record.status == FlowRunStatus.COMPLETED:
                return cleared
            state = dict(record.state or {})
            engine = state.get("ticket_engine")
            if (
                record.flow_type == "ticket_flow"
                and not force
                and isinstance(engine, dict)
                and str(engine.get("reason_code") or "")
                in _RESUME_SIGNAL_REQUIRED_REASON_CODES
            ):
                has_new_reply = self._has_new_user_reply_signal(
                    run_id=run_id, input_data=record.input_data
                )
                has_repo_change = self._repo_changed_since_pause(engine)
                if not has_new_reply and not has_repo_change:
                    raise ValueError(
                        "Run is paused on a blocking condition. Provide a new /flow reply, "
                        "change repository state, or resume with force."
                    )
            if isinstance(engine, dict):
                engine = dict(engine)
                if engine.get("reason_code") == "max_turns":
                    engine["total_turns"] = 0
                engine["status"] = "running"
                engine.pop("reason", None)
                engine.pop("reason_details", None)
                engine.pop("reason_code", None)
                engine.pop("pause_context", None)
                state["ticket_engine"] = engine
            state.pop("reason_summary", None)
            # Clear stale failure diagnostics when resuming a run.
            state.pop("failure", None)

            updated = self.store.update_flow_run_status(
                run_id=run_id,
                status=FlowRunStatus.RUNNING,
                state=state,
            )
            if updated:
                return updated

            updated = self.store.get_flow_run(run_id)
            if not updated:
                raise RuntimeError(f"Failed to get record for run {run_id}")
            return updated

    def _repo_root(self) -> Optional[Path]:
        if not self.db_path:
            return None
        return self.db_path.parent.parent

    def _repo_fingerprint(self) -> Optional[str]:
        repo_root = self._repo_root()
        if repo_root is None:
            return None
        try:
            head_proc = run_git(["rev-parse", "HEAD"], cwd=repo_root, check=True)
            status_proc = run_git(["status", "--porcelain"], cwd=repo_root, check=True)
            head = (head_proc.stdout or "").strip()
            status = (status_proc.stdout or "").strip()
            if not head:
                return None
            return f"{head}\n{status}"
        except Exception:
            return None

    def _has_new_user_reply_signal(
        self, *, run_id: str, input_data: dict[str, Any]
    ) -> bool:
        repo_root = self._repo_root()
        if repo_root is None:
            return False
        raw_workspace = input_data.get("workspace_root")
        if isinstance(raw_workspace, str) and raw_workspace.strip():
            workspace_root = Path(raw_workspace)
            if not workspace_root.is_absolute():
                workspace_root = (repo_root / workspace_root).resolve()
            else:
                workspace_root = workspace_root.resolve()
        else:
            workspace_root = repo_root
        runs_dir_raw = input_data.get("runs_dir")
        runs_dir = (
            Path(runs_dir_raw)
            if isinstance(runs_dir_raw, str) and runs_dir_raw
            else Path(".codex-autorunner/runs")
        )
        if not runs_dir.is_absolute():
            run_dir = workspace_root / runs_dir / run_id
        else:
            run_dir = runs_dir / run_id
        return (run_dir / "USER_REPLY.md").exists()

    def _repo_changed_since_pause(self, engine: dict[str, Any]) -> bool:
        pause_context = engine.get("pause_context")
        if not isinstance(pause_context, dict):
            return False
        paused_fingerprint = pause_context.get("repo_fingerprint")
        if not isinstance(paused_fingerprint, str) or not paused_fingerprint:
            return False
        current_fingerprint = self._repo_fingerprint()
        if not isinstance(current_fingerprint, str):
            return False
        return paused_fingerprint != current_fingerprint

    def get_status(self, run_id: str) -> Optional[FlowRunRecord]:
        return self.store.get_flow_run(run_id)

    def list_runs(self, status: Optional[FlowRunStatus] = None) -> list[FlowRunRecord]:
        return self.store.list_flow_runs(
            flow_type=self.definition.flow_type, status=status
        )

    async def stream_events(
        self, run_id: str, after_seq: Optional[int] = None
    ) -> AsyncGenerator[FlowEvent, None]:
        last_seq = after_seq

        while True:
            events = self.store.get_events(
                run_id=run_id,
                after_seq=last_seq,
                limit=100,
            )

            for event in events:
                yield event
                last_seq = event.seq

            record = self.store.get_flow_run(run_id)
            if (
                record
                and (record.status.is_terminal() or record.status.is_paused())
                and not events
            ):
                break

            await asyncio.sleep(0.5)

    def get_events(
        self, run_id: str, after_seq: Optional[int] = None
    ) -> list[FlowEvent]:
        return self.store.get_events(run_id=run_id, after_seq=after_seq)

    def add_event_listener(self, listener: Callable[[FlowEvent], None]) -> None:
        self._event_listeners.add(listener)

    def remove_event_listener(self, listener: Callable[[FlowEvent], None]) -> None:
        self._event_listeners.discard(listener)

    def add_lifecycle_event_listener(
        self, listener: Callable[[str, str, str, dict, str], None]
    ) -> None:
        self._lifecycle_event_listeners.add(listener)

    def remove_lifecycle_event_listener(
        self, listener: Callable[[str, str, str, dict, str], None]
    ) -> None:
        self._lifecycle_event_listeners.discard(listener)

    def _emit_lifecycle(
        self,
        event_type: str,
        repo_id: str,
        run_id: str,
        data: Dict[str, Any],
        origin: str,
    ) -> None:
        resolved_repo_id = self._repo_id or repo_id
        payload = data
        if resolved_repo_id and data.get("repo_id") != resolved_repo_id:
            payload = dict(data)
            payload["repo_id"] = resolved_repo_id
        for listener in self._lifecycle_event_listeners:
            try:
                listener(event_type, resolved_repo_id, run_id, payload, origin)
            except Exception as e:
                _logger.exception("Error in lifecycle event listener: %s", e)

    def _emit_to_lifecycle_store(
        self,
        event_type: str,
        repo_id: str,
        run_id: str,
        data: Dict[str, Any],
        origin: str,
    ) -> None:
        if self._lifecycle_emitter is None:
            return
        try:
            if event_type == "flow_paused":
                self._lifecycle_emitter.emit_flow_paused(
                    repo_id, run_id, data=data, origin=origin
                )
            elif event_type == "flow_completed":
                self._lifecycle_emitter.emit_flow_completed(
                    repo_id, run_id, data=data, origin=origin
                )
            elif event_type == "flow_failed":
                self._lifecycle_emitter.emit_flow_failed(
                    repo_id, run_id, data=data, origin=origin
                )
            elif event_type == "flow_stopped":
                self._lifecycle_emitter.emit_flow_stopped(
                    repo_id, run_id, data=data, origin=origin
                )
        except Exception as exc:
            _logger.exception("Error emitting to lifecycle store: %s", exc)

    def _emit_event(self, event: FlowEvent) -> None:
        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception as e:
                _logger.exception("Error in event listener: %s", e)

    def _prepare_artifacts_dir(self, run_id: str) -> Path:
        artifacts_dir = self.artifacts_root / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        return artifacts_dir

    def get_artifacts_dir(self, run_id: str) -> Optional[Path]:
        artifacts_dir = self.artifacts_root / run_id
        if artifacts_dir.exists():
            return artifacts_dir
        return None

    def get_artifacts(self, run_id: str) -> list:
        return self.store.get_artifacts(run_id)

    async def stream_events_since(
        self, run_id: str, start_seq: Optional[int] = None
    ) -> AsyncGenerator[FlowEvent, None]:
        async for event in self.stream_events(run_id, after_seq=start_seq):
            yield event
