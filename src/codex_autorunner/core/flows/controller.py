import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Set

from .definition import FlowDefinition
from .models import FlowEvent, FlowRunRecord, FlowRunStatus
from .runtime import FlowRuntime
from .store import FlowStore

_logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlowController:
    def __init__(
        self,
        definition: FlowDefinition,
        db_path: Path,
        artifacts_root: Path,
    ):
        self.definition = definition
        self.db_path = db_path
        self.artifacts_root = artifacts_root
        self.store = FlowStore(db_path)
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._event_listeners: Set[Callable[[FlowEvent], None]] = set()
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.store.initialize()

    def shutdown(self) -> None:
        for run_id, task in self._active_tasks.items():
            if not task.done():
                _logger.info("Cancelling active task for run %s", run_id)
                task.cancel()
        self.store.close()

    async def start_flow(
        self,
        input_data: Dict[str, Any],
        run_id: Optional[str] = None,
        initial_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FlowRunRecord:
        if run_id is None:
            run_id = str(uuid.uuid4())

        async with self._lock:
            if run_id in self._active_tasks:
                raise ValueError(f"Flow run {run_id} is already active")

            existing = self.store.get_flow_run(run_id)
            if existing:
                raise ValueError(f"Flow run {run_id} already exists")

            self._prepare_artifacts_dir(run_id)

            runtime = FlowRuntime(
                definition=self.definition,
                store=self.store,
                emit_event=self._emit_event,
            )

            task = asyncio.create_task(
                runtime.run_flow(
                    run_id=run_id,
                    input_data=input_data,
                    initial_state=initial_state,
                    metadata=metadata,
                )
            )
            task.add_done_callback(lambda t: self._active_tasks.pop(run_id, None))
            self._active_tasks[run_id] = task

            record = self.store.get_flow_run(run_id)
            if not record:
                raise RuntimeError(f"Failed to get record for run {run_id}")

            return record

    async def stop_flow(self, run_id: str) -> FlowRunRecord:
        record = self.store.set_stop_requested(run_id, True)
        if not record:
            raise ValueError(f"Flow run {run_id} not found")

        if record.status == FlowRunStatus.RUNNING:
            if run_id in self._active_tasks:
                task = self._active_tasks[run_id]
                try:
                    await asyncio.wait_for(task, timeout=30.0)
                except asyncio.TimeoutError:
                    _logger.warning(
                        "Flow run %s did not stop gracefully within timeout", run_id
                    )

        updated = self.store.get_flow_run(run_id)
        if not updated:
            raise RuntimeError(f"Failed to get record for run {run_id}")
        return updated

    async def resume_flow(self, run_id: str) -> FlowRunRecord:
        async with self._lock:
            if run_id in self._active_tasks:
                raise ValueError(f"Flow run {run_id} is already active")

            record = self.store.get_flow_run(run_id)
            if not record:
                raise ValueError(f"Flow run {run_id} not found")

            if record.status not in {FlowRunStatus.STOPPED, FlowRunStatus.FAILED}:
                raise ValueError(
                    f"Flow run {run_id} has status {record.status}, cannot resume"
                )

            runtime = FlowRuntime(
                definition=self.definition,
                store=self.store,
                emit_event=self._emit_event,
            )

            task = asyncio.create_task(runtime.resume_flow(run_id))
            task.add_done_callback(lambda t: self._active_tasks.pop(run_id, None))
            self._active_tasks[run_id] = task

            updated_record = self.store.get_flow_run(run_id)
            if not updated_record:
                raise RuntimeError(f"Failed to get record for run {run_id}")

            return updated_record

    def get_status(self, run_id: str) -> Optional[FlowRunRecord]:
        return self.store.get_flow_run(run_id)

    def list_runs(self, status: Optional[FlowRunStatus] = None) -> list[FlowRunRecord]:
        return self.store.list_flow_runs(
            flow_type=self.definition.flow_type, status=status
        )

    async def stream_events(
        self, run_id: str, after_timestamp: Optional[str] = None
    ) -> AsyncGenerator[FlowEvent, None]:
        last_timestamp = after_timestamp

        while True:
            events = self.store.get_events(
                run_id=run_id,
                after_timestamp=last_timestamp,
                limit=100,
            )

            for event in events:
                yield event
                last_timestamp = event.timestamp

            record = self.store.get_flow_run(run_id)
            if record and record.status.is_terminal():
                break

            await asyncio.sleep(0.5)

    def get_events(
        self, run_id: str, after_timestamp: Optional[str] = None
    ) -> list[FlowEvent]:
        return self.store.get_events(run_id=run_id, after_timestamp=after_timestamp)

    def add_event_listener(self, listener: Callable[[FlowEvent], None]) -> None:
        self._event_listeners.add(listener)

    def remove_event_listener(self, listener: Callable[[FlowEvent], None]) -> None:
        self._event_listeners.discard(listener)

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
        self, run_id: str, start_timestamp: Optional[str] = None
    ) -> AsyncGenerator[FlowEvent, None]:
        last_timestamp = start_timestamp

        while True:
            events = self.store.get_events(
                run_id=run_id,
                after_timestamp=last_timestamp,
            )

            for event in events:
                yield event
                last_timestamp = event.timestamp

            record = self.store.get_flow_run(run_id)
            if record and record.status.is_terminal():
                break

            await asyncio.sleep(0.5)
