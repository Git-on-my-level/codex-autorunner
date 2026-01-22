import logging
import uuid
from typing import Any, Callable, Dict, Optional, Set

from .definition import FlowDefinition, StepFn, StepOutcome
from .models import FlowEvent, FlowEventType, FlowRunRecord, FlowRunStatus
from .store import FlowStore, now_iso

_logger = logging.getLogger(__name__)


class FlowRuntime:
    def __init__(
        self,
        definition: FlowDefinition,
        store: FlowStore,
        emit_event: Optional[Callable[[FlowEvent], None]] = None,
    ):
        self.definition = definition
        self.store = store
        self.emit_event = emit_event
        self._stop_check_interval = 0.5

    def _emit(
        self,
        event_type: FlowEventType,
        run_id: str,
        data: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> None:
        event = self.store.create_event(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            event_type=event_type,
            data=data or {},
            step_id=step_id,
        )
        if self.emit_event:
            try:
                self.emit_event(event)
            except Exception as e:
                _logger.exception("Error emitting event: %s", e)

    async def run_flow(
        self,
        run_id: str,
        initial_state: Optional[Dict[str, Any]] = None,
    ) -> FlowRunRecord:
        record = self.store.get_flow_run(run_id)
        if not record:
            raise RuntimeError(f"Flow run {run_id} not found")

        if record.status.is_terminal() and record.status not in {
            FlowRunStatus.STOPPED,
            FlowRunStatus.FAILED,
        }:
            return record

        try:
            self.store.set_stop_requested(run_id, False)

            if record.status == FlowRunStatus.PENDING:
                self._emit(FlowEventType.FLOW_STARTED, run_id)
                now = now_iso()
                updated = self.store.update_flow_run_status(
                    run_id=run_id,
                    status=FlowRunStatus.RUNNING,
                    started_at=now,
                    state=initial_state if initial_state is not None else record.state,
                    current_step=record.current_step or self.definition.initial_step,
                )
                if not updated:
                    raise RuntimeError(f"Failed to start flow run {run_id}")
                record = updated
            else:
                self._emit(FlowEventType.FLOW_RESUMED, run_id)
                updated = self.store.update_flow_run_status(
                    run_id=run_id,
                    status=FlowRunStatus.RUNNING,
                    state=initial_state if initial_state is not None else record.state,
                )
                if updated:
                    record = updated

            next_steps: Set[str] = set()
            if record.current_step:
                next_steps.add(record.current_step)
            else:
                next_steps.add(self.definition.initial_step)

            while next_steps:
                latest = self.store.get_flow_run(run_id)
                if latest:
                    record = latest

                if record.stop_requested:
                    self._emit(FlowEventType.FLOW_STOPPED, run_id)
                    now = now_iso()
                    updated = self.store.update_flow_run_status(
                        run_id=run_id,
                        status=FlowRunStatus.STOPPED,
                        finished_at=now,
                    )
                    if not updated:
                        raise RuntimeError(f"Failed to stop flow run {run_id}")
                    record = updated
                    break

                step_id = next_steps.pop()

                record = await self._execute_step(record, step_id)

                if record.status.is_terminal():
                    break

                if record.status == FlowRunStatus.RUNNING:
                    if not next_steps and record.current_step:
                        next_steps = {record.current_step}

            return record

        except Exception as e:
            _logger.exception("Flow run %s failed with exception", run_id)
            self._emit(
                FlowEventType.FLOW_FAILED,
                run_id,
                data={"error": str(e)},
            )
            now = now_iso()
            updated = self.store.update_flow_run_status(
                run_id=run_id,
                status=FlowRunStatus.FAILED,
                finished_at=now,
                error_message=str(e),
            )
            if not updated:
                raise RuntimeError(
                    f"Failed to update flow run {run_id} to failed state"
                ) from e
            record = updated
            return record

    async def _execute_step(
        self,
        record: FlowRunRecord,
        step_id: str,
    ) -> FlowRunRecord:
        if step_id not in self.definition.steps:
            raise ValueError(f"Step '{step_id}' not found in flow definition")

        step_fn: StepFn = self.definition.steps[step_id]

        self._emit(
            FlowEventType.STEP_STARTED,
            record.id,
            data={"step_id": step_id},
            step_id=step_id,
        )

        updated = self.store.update_current_step(
            run_id=record.id,
            current_step=step_id,
        )
        if not updated:
            raise RuntimeError(f"Failed to update current step to {step_id}")
        record = updated

        try:
            outcome: StepOutcome = await step_fn(record, record.input_data)

            if outcome.output:
                record.state.update(outcome.output)

            if outcome.status == FlowRunStatus.RUNNING:
                self._emit(
                    FlowEventType.STEP_COMPLETED,
                    record.id,
                    data={"step_id": step_id, "next_steps": list(outcome.next_steps)},
                    step_id=step_id,
                )

                if outcome.next_steps:
                    next_step = min(outcome.next_steps)
                else:
                    next_step = None

                updated = self.store.update_flow_run_status(
                    run_id=record.id,
                    status=FlowRunStatus.RUNNING,
                    state=record.state,
                    current_step=next_step,
                )
                if not updated:
                    raise RuntimeError(
                        f"Failed to update flow run after step {step_id}"
                    )
                record = updated

            elif outcome.status == FlowRunStatus.COMPLETED:
                self._emit(
                    FlowEventType.STEP_COMPLETED,
                    record.id,
                    data={"step_id": step_id, "status": "completed"},
                    step_id=step_id,
                )
                self._emit(FlowEventType.FLOW_COMPLETED, record.id)

                now = now_iso()
                updated = self.store.update_flow_run_status(
                    run_id=record.id,
                    status=FlowRunStatus.COMPLETED,
                    finished_at=now,
                    state=record.state,
                    current_step=None,
                )
                if not updated:
                    raise RuntimeError(
                        f"Failed to update flow run after step {step_id}"
                    )
                record = updated

            elif outcome.status == FlowRunStatus.FAILED:
                self._emit(
                    FlowEventType.STEP_FAILED,
                    record.id,
                    data={"step_id": step_id, "error": outcome.error},
                    step_id=step_id,
                )

                now = now_iso()
                updated = self.store.update_flow_run_status(
                    run_id=record.id,
                    status=FlowRunStatus.FAILED,
                    finished_at=now,
                    error_message=outcome.error,
                    current_step=None,
                )
                if not updated:
                    raise RuntimeError(
                        f"Failed to update flow run after step {step_id}"
                    )
                record = updated

            elif outcome.status == FlowRunStatus.STOPPED:
                self._emit(
                    FlowEventType.STEP_COMPLETED,
                    record.id,
                    data={"step_id": step_id, "status": "stopped"},
                    step_id=step_id,
                )

                now = now_iso()
                updated = self.store.update_flow_run_status(
                    run_id=record.id,
                    status=FlowRunStatus.STOPPED,
                    finished_at=now,
                    state=record.state,
                    current_step=None,
                )
                if not updated:
                    raise RuntimeError(
                        f"Failed to update flow run after step {step_id}"
                    )
                record = updated

            return record

        except Exception as e:
            _logger.exception("Step %s failed with exception", step_id)
            self._emit(
                FlowEventType.STEP_FAILED,
                record.id,
                data={"step_id": step_id, "error": str(e)},
                step_id=step_id,
            )

            now = now_iso()
            updated = self.store.update_flow_run_status(
                run_id=record.id,
                status=FlowRunStatus.FAILED,
                finished_at=now,
                error_message=str(e),
                current_step=None,
            )
            if not updated:
                raise RuntimeError(
                    f"Failed to update flow run after step {step_id}"
                ) from e
            record = updated
            return record
