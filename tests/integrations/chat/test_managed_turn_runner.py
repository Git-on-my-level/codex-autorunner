from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import codex_autorunner.integrations.chat.managed_turn_runner as managed_turn_runner_module
from codex_autorunner.core.orchestration.models import (
    ExecutionRecord,
    MessageRequest,
    ThreadTarget,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadExecution
from codex_autorunner.integrations.chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
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
