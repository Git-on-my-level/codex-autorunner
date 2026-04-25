import inspect
import logging
import sqlite3
import uuid
from typing import Any, Callable, Dict, Optional, Set, cast

from ..lifecycle_events import LifecycleEventType
from .definition import STEP_WANTS_EMIT_ATTR, FlowDefinition, StepFn, StepFn2, StepFn3
from .failure_diagnostics import (
    CANONICAL_FAILURE_REASON_CODE_FIELD,
    ensure_failure_payload,
)
from .flow_transition_telemetry import (
    emit_failure_projection,
    emit_runtime_transition,
)
from .lifecycle_reducer import (
    NO_CHANGE,
    EffectKind,
    FlowTrigger,
    TransitionResult,
    TriggerKind,
    reduce_flow_lifecycle,
)
from .models import FlowEvent, FlowEventType, FlowRunRecord, FlowRunStatus
from .store import FlowStore, now_iso

_logger = logging.getLogger(__name__)


LifecycleEventCallback = Optional[
    Callable[[LifecycleEventType, str, str, Dict[str, Any], str], None]
]


class FlowRuntime:
    def __init__(
        self,
        definition: FlowDefinition,
        store: FlowStore,
        emit_event: Optional[Callable[[FlowEvent], None]] = None,
        emit_lifecycle_event: LifecycleEventCallback = None,
    ):
        self.definition = definition
        self.store = store
        self.emit_event = emit_event
        self.emit_lifecycle_event = emit_lifecycle_event
        self._stop_check_interval = 0.5

    def _build_transition_token(
        self, event_type: LifecycleEventType, record: FlowRunRecord
    ) -> str:
        status_value = (
            record.status.value
            if isinstance(record.status, FlowRunStatus)
            else str(record.status)
        )
        finished_at = record.finished_at or ""
        return f"{event_type.value}:{record.id}:{status_value}:{finished_at}"

    def _with_transition_metadata(
        self,
        event_type: LifecycleEventType,
        record: FlowRunRecord,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = dict(data or {})
        token = self._build_transition_token(event_type, record)
        payload.setdefault("transition_token", token)
        payload.setdefault(
            "transition_idempotency_key",
            f"lifecycle:{event_type.value}:{record.id}:{token}",
        )
        return payload

    def _emit_lifecycle(
        self,
        event_type: LifecycleEventType,
        repo_id: str,
        run_id: str,
        data: Dict[str, Any],
        origin: str = "runner",
    ) -> None:
        if self.emit_lifecycle_event:
            try:
                self.emit_lifecycle_event(event_type, repo_id, run_id, data, origin)
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                sqlite3.Error,
            ) as exc:
                _logger.exception("Error emitting lifecycle event: %s", exc)

    def _emit(
        self,
        event_type: FlowEventType,
        run_id: str,
        data: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None,
    ) -> None:
        if event_type == FlowEventType.APP_SERVER_EVENT:
            event = self.store.create_telemetry(
                telemetry_id=str(uuid.uuid4()),
                run_id=run_id,
                event_type=event_type,
                data=data or {},
            )
        else:
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
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                sqlite3.Error,
            ) as e:
                _logger.exception("Error emitting event: %s", e)

    def _apply_transition(
        self,
        record: FlowRunRecord,
        result: TransitionResult,
        run_id: str,
        *,
        trigger_kind: Optional[str] = None,
    ) -> FlowRunRecord:
        previous_status = record.status
        has_failure_enrichment = False

        for effect in result.effects:
            if effect.kind == EffectKind.EMIT_FLOW_EVENT:
                event_type = FlowEventType(effect.event_type_name)
                self._emit(event_type, run_id, data=effect.data, step_id=effect.step_id)

        state = (
            result.state if result.state is not NO_CHANGE else dict(record.state or {})
        )
        for effect in result.effects:
            if effect.kind == EffectKind.ENRICH_FAILURE_PAYLOAD:
                has_failure_enrichment = True
                state = ensure_failure_payload(
                    state,
                    record=record,
                    step_id=effect.step_id,
                    error_message=effect.error_message,
                    store=self.store,
                    note=effect.note,
                    failed_at=result.finished_at or now_iso(),
                )

        kwargs: Dict[str, Any] = {"run_id": run_id, "status": result.status}
        if result.state is not NO_CHANGE:
            kwargs["state"] = state
        if result.current_step is not NO_CHANGE:
            kwargs["current_step"] = result.current_step
        if result.started_at is not NO_CHANGE:
            kwargs["started_at"] = result.started_at
        if result.finished_at is not NO_CHANGE:
            kwargs["finished_at"] = result.finished_at
        if result.error_message is not NO_CHANGE:
            kwargs["error_message"] = result.error_message
        updated = self.store.update_flow_run_status(**kwargs)
        if not updated:
            raise RuntimeError(f"Failed to update flow run {run_id}")
        record = updated

        emit_runtime_transition(
            store=self.store,
            run_id=run_id,
            previous_status=previous_status,
            resulting_status=result.status,
            trigger=trigger_kind or result.note or "unknown",
            note=result.note or "",
            step_id=(
                result.current_step if result.current_step is not NO_CHANGE else None
            ),
            error_message=(
                result.error_message if result.error_message is not NO_CHANGE else None
            ),
        )

        if has_failure_enrichment and isinstance(state, dict):
            failure = state.get("failure")
            reason_code = (
                failure.get(CANONICAL_FAILURE_REASON_CODE_FIELD)
                if isinstance(failure, dict)
                else None
            )
            emit_failure_projection(
                store=self.store,
                run_id=run_id,
                status=result.status,
                failure_reason_code=reason_code,
                error_message=(
                    result.error_message
                    if result.error_message is not NO_CHANGE
                    else None
                ),
                origin="runtime",
            )

        for effect in result.effects:
            if effect.kind == EffectKind.EMIT_LIFECYCLE_EVENT:
                lifecycle_type = LifecycleEventType(effect.event_type_name)
                self._emit_lifecycle(
                    lifecycle_type,
                    "",
                    run_id,
                    self._with_transition_metadata(lifecycle_type, record, effect.data),
                )

        return record

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
            now = now_iso()

            if record.status == FlowRunStatus.PENDING:
                trigger = FlowTrigger(
                    kind=TriggerKind.FLOW_START,
                    state_output=initial_state if initial_state is not None else {},
                )
                result = reduce_flow_lifecycle(
                    record.status,
                    record.state,
                    trigger,
                    now=now,
                    current_step=record.current_step,
                    initial_step=self.definition.initial_step,
                )
                record = self._apply_transition(
                    record, result, run_id, trigger_kind="flow_start"
                )
            else:
                trigger = FlowTrigger(
                    kind=TriggerKind.FLOW_RESUME,
                    state_output=initial_state if initial_state is not None else {},
                )
                result = reduce_flow_lifecycle(
                    record.status,
                    record.state,
                    trigger,
                    now=now,
                    current_step=record.current_step,
                    initial_step=self.definition.initial_step,
                )
                record = self._apply_transition(
                    record, result, run_id, trigger_kind="flow_resume"
                )

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
                    now = now_iso()
                    trigger = FlowTrigger(kind=TriggerKind.STOP_REQUESTED)
                    result = reduce_flow_lifecycle(
                        record.status,
                        record.state,
                        trigger,
                        now=now,
                    )
                    record = self._apply_transition(
                        record, result, run_id, trigger_kind="stop_requested"
                    )
                    break

                step_id = next_steps.pop()

                record = await self._execute_step(record, step_id)

                if record.status.is_terminal() or record.status == FlowRunStatus.PAUSED:
                    break

                if record.status == FlowRunStatus.RUNNING:
                    if not next_steps and record.current_step:
                        next_steps = {record.current_step}

            return record

        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            sqlite3.Error,
            AttributeError,
            KeyError,
        ) as e:
            _logger.exception("Flow run %s failed with exception", run_id)
            now = now_iso()
            trigger = FlowTrigger(
                kind=TriggerKind.FLOW_EXCEPTION,
                step_id=record.current_step,
                error_message=str(e),
            )
            result = reduce_flow_lifecycle(
                record.status,
                record.state,
                trigger,
                now=now,
                current_step=record.current_step,
            )
            record = self._apply_transition(
                record, result, run_id, trigger_kind="flow_exception"
            )
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
            data={"step_id": step_id, "step_name": step_id},
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

            def _bound_emit(event_type: FlowEventType, data: Dict[str, Any]) -> None:
                self._emit(
                    event_type,
                    record.id,
                    data=data,
                    step_id=step_id,
                )

            def _step_accepts_emit() -> bool:
                marker_value = getattr(step_fn, STEP_WANTS_EMIT_ATTR, None)
                if marker_value is not None:
                    return bool(marker_value)
                try:
                    sig = inspect.signature(step_fn)
                except (TypeError, ValueError):
                    return False
                params = list(sig.parameters.values())
                if any(
                    p.kind
                    in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                    for p in params
                ):
                    return True
                positional = [
                    p
                    for p in params
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
                return len(positional) >= 3

            if _step_accepts_emit():
                outcome = await cast(StepFn3, step_fn)(
                    record, record.input_data, _bound_emit
                )
            else:
                outcome = await cast(StepFn2, step_fn)(record, record.input_data)

            now = now_iso()
            state_output = dict(outcome.output) if outcome.output else {}

            if outcome.status == FlowRunStatus.RUNNING:
                trigger = FlowTrigger(
                    kind=TriggerKind.STEP_CONTINUE,
                    step_id=step_id,
                    next_steps=frozenset(outcome.next_steps),
                    state_output=state_output,
                )
                result = reduce_flow_lifecycle(
                    record.status, record.state, trigger, now=now, current_step=step_id
                )
                record = self._apply_transition(
                    record, result, record.id, trigger_kind="step_continue"
                )

            elif outcome.status == FlowRunStatus.COMPLETED:
                trigger = FlowTrigger(
                    kind=TriggerKind.STEP_COMPLETE,
                    step_id=step_id,
                    state_output=state_output,
                )
                result = reduce_flow_lifecycle(
                    record.status, record.state, trigger, now=now, current_step=step_id
                )
                record = self._apply_transition(
                    record, result, record.id, trigger_kind="step_complete"
                )

            elif outcome.status == FlowRunStatus.FAILED:
                trigger = FlowTrigger(
                    kind=TriggerKind.STEP_FAIL,
                    step_id=step_id,
                    error_message=outcome.error,
                    state_output=state_output,
                )
                result = reduce_flow_lifecycle(
                    record.status, record.state, trigger, now=now, current_step=step_id
                )
                record = self._apply_transition(
                    record, result, record.id, trigger_kind="step_fail"
                )

            elif outcome.status == FlowRunStatus.STOPPED:
                trigger = FlowTrigger(
                    kind=TriggerKind.STEP_STOP,
                    step_id=step_id,
                    state_output=state_output,
                )
                result = reduce_flow_lifecycle(
                    record.status, record.state, trigger, now=now, current_step=step_id
                )
                record = self._apply_transition(
                    record, result, record.id, trigger_kind="step_stop"
                )

            elif outcome.status == FlowRunStatus.PAUSED:
                trigger = FlowTrigger(
                    kind=TriggerKind.STEP_PAUSE,
                    step_id=step_id,
                    state_output=state_output,
                )
                result = reduce_flow_lifecycle(
                    record.status, record.state, trigger, now=now, current_step=step_id
                )
                record = self._apply_transition(
                    record, result, record.id, trigger_kind="step_pause"
                )

            return record

        except Exception as e:
            _logger.exception("Step %s failed with exception", step_id)
            now = now_iso()
            trigger = FlowTrigger(
                kind=TriggerKind.STEP_EXCEPTION,
                step_id=step_id,
                error_message=str(e),
            )
            result = reduce_flow_lifecycle(
                record.status, record.state, trigger, now=now, current_step=step_id
            )
            record = self._apply_transition(
                record, result, record.id, trigger_kind="step_exception"
            )
            return record
