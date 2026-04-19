from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import codex_autorunner.integrations.chat.managed_turn_runner as managed_turn_runner_module
from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryState,
)
from codex_autorunner.core.orchestration.models import (
    ExecutionRecord,
    MessageRequest,
    ThreadTarget,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadExecution
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadExecutionFlowResult,
    ManagedThreadFinalizationResult,
    ManagedThreadSubmissionResult,
)


def _build_started_execution(
    tmp_path: Path, *, status: str = "running"
) -> RuntimeThreadExecution:
    return RuntimeThreadExecution(
        service=SimpleNamespace(),
        harness=SimpleNamespace(),
        thread=ThreadTarget(
            thread_target_id="thread-1",
            agent_id="codex",
            workspace_root=str(tmp_path),
            lifecycle_status="active",
            backend_thread_id="backend-thread-1",
        ),
        execution=ExecutionRecord(
            execution_id="exec-1",
            target_id="thread-1",
            target_kind="thread",
            status=status,
            backend_id="backend-turn-1",
        ),
        workspace_root=tmp_path,
        request=MessageRequest(
            target_id="thread-1",
            target_kind="thread",
            message_text="hello",
        ),
    )


@pytest.mark.anyio
async def test_run_managed_surface_turn_routes_queued_turns_through_shared_queue_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path, status="queued")
    ensured: dict[str, Any] = {}
    callbacks: list[str] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=True)

    def _ensure_queue_worker(**kwargs: Any) -> None:
        ensured.update(kwargs)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        *,
        ensure_queue_worker: Any,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, kwargs
        ensure_queue_worker()
        return ManagedThreadExecutionFlowResult(
            started_execution=submission.started_execution,
            queued=True,
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=_ensure_queue_worker,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            hooks=ManagedThreadCoordinatorHooks(
                deliver_result=lambda finalized: callbacks.append(finalized.status)
            ),
            queue=managed_turn_runner_module.ManagedSurfaceQueueConfig(
                task_map={},
                managed_thread_id="thread-1",
                spawn_task=lambda coro: (_ for _ in ()).throw(
                    AssertionError("spawn_task should not run in this test")
                ),
            ),
            on_queued=lambda flow: "queued",
            on_finalized=lambda flow, finalized: "finalized",
        ),
    )

    assert result == "queued"
    assert ensured["managed_thread_id"] == "thread-1"
    assert callable(ensured["hooks"].deliver_result)
    assert callbacks == []


@pytest.mark.anyio
async def test_run_managed_surface_turn_does_not_treat_finalize_callback_errors_as_submission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    submission_error_calls: list[str] = []
    after_completion_calls: list[bool] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, submission, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=ManagedThreadFinalizationResult(
                status="ok",
                assistant_text="done",
                error=None,
                managed_thread_id="thread-1",
                managed_turn_id="exec-1",
                backend_thread_id="backend-thread-1",
            ),
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    with pytest.raises(ValueError, match="finalize boom"):
        await managed_turn_runner_module.run_managed_surface_turn(
            started.request,
            config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[None](
                coordinator=SimpleNamespace(
                    submit_execution=_submit_execution,
                    ensure_queue_worker=lambda **kwargs: None,
                ),
                client_request_id="req-1",
                sandbox_policy=None,
                on_submission_error=lambda exc: submission_error_calls.append(str(exc)),
                on_finalized=lambda flow, finalized: (_ for _ in ()).throw(
                    ValueError("finalize boom")
                ),
                after_completion=lambda flow: after_completion_calls.append(
                    flow is not None
                ),
            ),
        )

    assert submission_error_calls == []
    assert after_completion_calls == [True]


@pytest.mark.anyio
async def test_run_managed_surface_turn_reraises_cancelled_submission(
    tmp_path: Path,
) -> None:
    started = _build_started_execution(tmp_path)
    submission_error_calls: list[str] = []
    after_completion_calls: list[bool] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await managed_turn_runner_module.run_managed_surface_turn(
            started.request,
            config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[None](
                coordinator=SimpleNamespace(
                    submit_execution=_submit_execution,
                    ensure_queue_worker=lambda **kwargs: None,
                ),
                client_request_id="req-1",
                sandbox_policy=None,
                on_submission_error=lambda exc: submission_error_calls.append(str(exc)),
                after_completion=lambda flow: after_completion_calls.append(
                    flow is not None
                ),
            ),
        )

    assert submission_error_calls == []
    assert after_completion_calls == [False]


@pytest.mark.anyio
async def test_run_managed_surface_turn_continues_after_post_submit_callback_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    submission_error_calls: list[str] = []
    post_submit_error_calls: list[str] = []
    after_completion_calls: list[bool] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, submission, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=ManagedThreadFinalizationResult(
                status="ok",
                assistant_text="done",
                error=None,
                managed_thread_id="thread-1",
                managed_turn_id="exec-1",
                backend_thread_id="backend-thread-1",
            ),
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=lambda **kwargs: None,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            after_submission=lambda submission: (_ for _ in ()).throw(
                ValueError("after submit boom")
            ),
            on_submission_error=lambda exc: submission_error_calls.append(str(exc)),
            on_after_submission_error=lambda submission, exc: post_submit_error_calls.append(
                str(exc)
            ),
            on_finalized=lambda flow, finalized: finalized.assistant_text,
            after_completion=lambda flow: after_completion_calls.append(
                flow is not None
            ),
        ),
    )

    assert result == "done"
    assert submission_error_calls == []
    assert post_submit_error_calls == ["after submit boom"]
    assert after_completion_calls == [True]


class _StubAdapter:
    def __init__(self) -> None:
        self.adapter_key = "test"
        self.calls: list[ManagedThreadDeliveryRecord] = []

    async def deliver_managed_thread_record(
        self, record: ManagedThreadDeliveryRecord, *, claim: Any
    ) -> ManagedThreadDeliveryAttemptResult:
        self.calls.append(record)
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DELIVERED,
        )


class _StubEngine:
    def __init__(self) -> None:
        self.intents: list[Any] = []
        self.claimed_ids: list[str] = []
        self.results: list[Any] = []

    def create_intent(self, intent: Any) -> Any:
        self.intents.append(intent)
        record = ManagedThreadDeliveryRecord(
            delivery_id=intent.delivery_id,
            managed_thread_id=intent.managed_thread_id,
            managed_turn_id=intent.managed_turn_id,
            idempotency_key=intent.idempotency_key,
            target=intent.target,
            envelope=intent.envelope,
            state=ManagedThreadDeliveryState.PENDING,
            attempt_count=0,
            created_at="2026-01-01T00:00:00Z",
        )
        return SimpleNamespace(record=record, inserted=True)

    def claim_delivery(self, delivery_id: str, **kwargs: Any) -> Any:
        self.claimed_ids.append(delivery_id)
        matching = [i for i in self.intents if i.delivery_id == delivery_id]
        if not matching:
            return None
        intent = matching[0]
        record = ManagedThreadDeliveryRecord(
            delivery_id=intent.delivery_id,
            managed_thread_id=intent.managed_thread_id,
            managed_turn_id=intent.managed_turn_id,
            idempotency_key=intent.idempotency_key,
            target=intent.target,
            envelope=intent.envelope,
            state=ManagedThreadDeliveryState.CLAIMED,
            attempt_count=0,
            created_at="2026-01-01T00:00:00Z",
            claimed_at="2026-01-01T00:00:00Z",
            claim_token="claim-token-1",
        )
        return SimpleNamespace(record=record, claim_token="claim-token-1")

    def record_attempt_result(
        self, delivery_id: str, *, claim_token: str, result: Any
    ) -> None:
        self.results.append((delivery_id, claim_token, result))


def _make_durable_hooks(tmp_path: Path) -> ManagedThreadDurableDeliveryHooks:
    from codex_autorunner.integrations.chat.managed_thread_turns import (
        ManagedThreadSurfaceInfo,
        build_managed_thread_delivery_intent,
    )

    adapter = _StubAdapter()
    engine = _StubEngine()
    surface = ManagedThreadSurfaceInfo(
        log_label="Test",
        surface_kind="test",
        surface_key="test-key",
    )

    def _build_intent(
        finalized: ManagedThreadFinalizationResult,
    ) -> Any:
        if finalized.status == "interrupted":
            return None
        return build_managed_thread_delivery_intent(
            finalized,
            surface=surface,
            transport_target={"channel": "test"},
        )

    return ManagedThreadDurableDeliveryHooks(
        engine=engine,
        adapter=adapter,
        build_delivery_intent=_build_intent,
    )


@pytest.mark.anyio
async def test_direct_turn_creates_durable_delivery_record_before_on_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    finalized_result = ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="agent response",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-thread-1",
    )
    on_finalized_calls: list[str] = []
    delivery_hooks = _make_durable_hooks(tmp_path)

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=finalized_result,
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=lambda **kwargs: None,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            hooks=ManagedThreadCoordinatorHooks(
                durable_delivery=delivery_hooks,
            ),
            on_finalized=lambda flow, finalized: (
                on_finalized_calls.append(finalized.status),
                "ok-result",
            )[1],
        ),
    )

    assert result == "ok-result"
    assert len(delivery_hooks.engine.intents) == 1
    assert delivery_hooks.engine.intents[0].managed_thread_id == "thread-1"
    assert len(delivery_hooks.adapter.calls) == 1
    assert on_finalized_calls == ["ok"]


@pytest.mark.anyio
async def test_direct_turn_durable_delivery_cancellation_leaves_replayable_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    finalized_result = ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="agent response",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-thread-1",
    )
    delivery_hooks = _make_durable_hooks(tmp_path)
    delivery_hooks.engine.claim_delivery = lambda delivery_id, **kw: SimpleNamespace(
        record=SimpleNamespace(
            delivery_id=delivery_id,
            envelope=SimpleNamespace(assistant_text="agent response"),
        ),
        claim_token="ct",
    )

    async def _cancel_adapter(
        record: Any, *, claim: Any
    ) -> ManagedThreadDeliveryAttemptResult:
        raise asyncio.CancelledError()

    delivery_hooks.adapter.deliver_managed_thread_record = _cancel_adapter
    delivery_hooks.engine.record_attempt_result = lambda *a, **kw: None

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=finalized_result,
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    with pytest.raises(asyncio.CancelledError):
        await managed_turn_runner_module.run_managed_surface_turn(
            started.request,
            config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[None](
                coordinator=SimpleNamespace(
                    submit_execution=_submit_execution,
                    ensure_queue_worker=lambda **kwargs: None,
                ),
                client_request_id="req-1",
                sandbox_policy=None,
                hooks=ManagedThreadCoordinatorHooks(
                    durable_delivery=delivery_hooks,
                ),
                on_finalized=lambda flow, finalized: None,
            ),
        )

    assert len(delivery_hooks.engine.intents) == 1


@pytest.mark.anyio
async def test_direct_turn_without_durable_delivery_uses_on_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    finalized_result = ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="agent response",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-thread-1",
    )
    on_finalized_messages: list[str] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=finalized_result,
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=lambda **kwargs: None,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            hooks=ManagedThreadCoordinatorHooks(),
            on_finalized=lambda flow, finalized: (
                on_finalized_messages.append(finalized.assistant_text),
                finalized.assistant_text,
            )[1],
        ),
    )

    assert result == "agent response"
    assert on_finalized_messages == ["agent response"]


@pytest.mark.anyio
async def test_direct_turn_durable_delivery_failure_does_not_block_on_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    finalized_result = ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="agent response",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-thread-1",
    )
    delivery_hooks = _make_durable_hooks(tmp_path)

    async def _failing_adapter(
        record: Any, *, claim: Any
    ) -> ManagedThreadDeliveryAttemptResult:
        raise RuntimeError("transport failure")

    delivery_hooks.adapter.deliver_managed_thread_record = _failing_adapter
    delivery_hooks.engine.record_attempt_result = lambda *a, **kw: None

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=finalized_result,
        )

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )

    on_finalized_calls: list[str] = []

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=lambda **kwargs: None,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            hooks=ManagedThreadCoordinatorHooks(
                durable_delivery=delivery_hooks,
            ),
            on_finalized=lambda flow, finalized: (
                on_finalized_calls.append(finalized.status),
                "recovered",
            )[1],
        ),
    )

    assert result == "recovered"
    assert len(delivery_hooks.engine.intents) == 1
    assert on_finalized_calls == ["ok"]


@pytest.mark.anyio
async def test_direct_turn_retry_scheduled_delivery_does_not_mark_delivery_performed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    finalized_result = ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="agent response",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id="backend-thread-1",
    )
    observed_flags: list[bool] = []

    async def _submit_execution(
        *args: Any, **kwargs: Any
    ) -> ManagedThreadSubmissionResult:
        _ = args, kwargs
        return ManagedThreadSubmissionResult(started_execution=started, queued=False)

    async def _fake_complete(
        coordinator: Any,
        submission: ManagedThreadSubmissionResult,
        **kwargs: Any,
    ) -> ManagedThreadExecutionFlowResult:
        _ = coordinator, submission, kwargs
        return ManagedThreadExecutionFlowResult(
            started_execution=started,
            queued=False,
            finalized=finalized_result,
        )

    async def _retry_scheduled_handoff(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return SimpleNamespace(state=ManagedThreadDeliveryState.RETRY_SCHEDULED)

    monkeypatch.setattr(
        managed_turn_runner_module,
        "complete_managed_thread_execution",
        _fake_complete,
    )
    monkeypatch.setattr(
        managed_turn_runner_module,
        "handoff_managed_thread_final_delivery",
        _retry_scheduled_handoff,
    )

    result = await managed_turn_runner_module.run_managed_surface_turn(
        started.request,
        config=managed_turn_runner_module.ManagedSurfaceRunnerConfig[str](
            coordinator=SimpleNamespace(
                submit_execution=_submit_execution,
                ensure_queue_worker=lambda **kwargs: None,
            ),
            client_request_id="req-1",
            sandbox_policy=None,
            hooks=ManagedThreadCoordinatorHooks(
                durable_delivery=SimpleNamespace(),
            ),
            on_finalized=lambda flow, finalized: (
                observed_flags.append(
                    (
                        flow.durable_delivery_performed,
                        flow.durable_delivery_pending,
                    )
                ),
                finalized.assistant_text,
            )[1],
        ),
    )

    assert result == "agent response"
    assert observed_flags == [(False, True)]
