from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.integrations.chat.managed_thread_turns as managed_thread_turns_module
from codex_autorunner.core.orchestration.models import (
    ExecutionRecord,
    MessageRequest,
    ThreadTarget,
)
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadExecution


def _build_started_execution(tmp_path: Path) -> RuntimeThreadExecution:
    return RuntimeThreadExecution(
        service=SimpleNamespace(),
        harness=SimpleNamespace(),
        thread=ThreadTarget(
            thread_target_id="thread-1",
            agent_id="codex",
            workspace_root=str(tmp_path),
            lifecycle_status="active",
        ),
        execution=ExecutionRecord(
            execution_id="exec-1",
            target_id="thread-1",
            target_kind="thread",
            status="running",
        ),
        workspace_root=tmp_path,
        request=MessageRequest(
            target_id="thread-1",
            target_kind="thread",
            message_text="hello",
        ),
    )


@pytest.mark.anyio
async def test_managed_thread_turn_coordinator_runs_lifecycle_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    events: list[Any] = []

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["started"] is started
        assert kwargs["runtime_event_state"] is not None
        assert kwargs["on_progress_event"] is progress_handler
        events.append("finalize")
        return {"status": "ok", "managed_turn_id": "exec-1"}

    def _on_started(started_execution: RuntimeThreadExecution) -> None:
        assert started_execution is started
        events.append("started")

    async def _on_finished(started_execution: RuntimeThreadExecution) -> None:
        assert started_execution is started
        events.append("finished")

    async def progress_handler(run_event: Any) -> None:
        _ = run_event

    monkeypatch.setattr(
        managed_thread_turns_module,
        "finalize_managed_thread_execution",
        _fake_finalize,
    )

    coordinator = managed_thread_turns_module.ManagedThreadTurnCoordinator(
        orchestration_service=SimpleNamespace(),
        state_root=tmp_path,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Test",
            surface_kind="test",
            surface_key="surface-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="public",
            timeout_error="timeout",
            interrupted_error="interrupted",
            timeout_seconds=30,
        ),
        logger=logging.getLogger("test"),
        turn_preview="preview",
    )

    result = await coordinator.run_started_execution(
        started,
        hooks=managed_thread_turns_module.ManagedThreadCoordinatorHooks(
            on_execution_started=_on_started,
            on_execution_finished=_on_finished,
            on_progress_event=progress_handler,
        ),
        runtime_event_state=RuntimeThreadRunEventState(),
    )

    assert result == {"status": "ok", "managed_turn_id": "exec-1"}
    assert events == ["started", "finalize", "finished"]


@pytest.mark.anyio
async def test_managed_thread_turn_coordinator_queue_worker_uses_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    events: list[Any] = []
    begin_calls = 0

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["started"] is started
        events.append("finalize")
        return {
            "status": "ok",
            "managed_turn_id": started.execution.execution_id,
        }

    async def _fake_begin_next(
        orchestration_service: object,
        managed_thread_id: str,
    ) -> Optional[RuntimeThreadExecution]:
        nonlocal begin_calls
        _ = orchestration_service, managed_thread_id
        if begin_calls == 0:
            begin_calls += 1
            return started
        return None

    async def _deliver_result(finalized: dict[str, Any]) -> None:
        events.append(("deliver", finalized["managed_turn_id"]))

    async def _run_with_indicator(work: Any) -> None:
        events.append("indicator:start")
        await work()
        events.append("indicator:end")

    monkeypatch.setattr(
        managed_thread_turns_module,
        "finalize_managed_thread_execution",
        _fake_finalize,
    )

    task_map: dict[str, asyncio.Task[Any]] = {}
    spawned_tasks: list[asyncio.Task[Any]] = []
    orchestration_service = SimpleNamespace(
        get_running_execution=lambda managed_thread_id: None,
    )
    coordinator = managed_thread_turns_module.ManagedThreadTurnCoordinator(
        orchestration_service=orchestration_service,
        state_root=tmp_path,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Test",
            surface_kind="test",
            surface_key="surface-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="public",
            timeout_error="timeout",
            interrupted_error="interrupted",
            timeout_seconds=30,
        ),
        logger=logging.getLogger("test"),
        turn_preview="preview",
    )

    coordinator.ensure_queue_worker(
        task_map=task_map,
        managed_thread_id="thread-1",
        spawn_task=lambda coro: spawned_tasks.append(asyncio.create_task(coro))
        or spawned_tasks[-1],
        hooks=managed_thread_turns_module.ManagedThreadCoordinatorHooks(
            on_execution_started=lambda started_execution: events.append("started"),
            on_execution_finished=lambda started_execution: events.append("finished"),
            deliver_result=_deliver_result,
            run_with_indicator=_run_with_indicator,
        ),
        begin_next_execution=_fake_begin_next,
    )

    await asyncio.gather(*spawned_tasks)

    assert events == [
        "indicator:start",
        "started",
        "finalize",
        "finished",
        ("deliver", "exec-1"),
        "indicator:end",
    ]
    assert task_map == {}


@pytest.mark.anyio
async def test_complete_managed_thread_execution_runs_direct_hooks_and_ensures_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    events: list[Any] = []

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["started"] is started
        events.append("finalize")
        return {"status": "ok", "managed_turn_id": started.execution.execution_id}

    monkeypatch.setattr(
        managed_thread_turns_module,
        "finalize_managed_thread_execution",
        _fake_finalize,
    )

    coordinator = managed_thread_turns_module.ManagedThreadTurnCoordinator(
        orchestration_service=SimpleNamespace(),
        state_root=tmp_path,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Test",
            surface_kind="test",
            surface_key="surface-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="public",
            timeout_error="timeout",
            interrupted_error="interrupted",
            timeout_seconds=30,
        ),
        logger=logging.getLogger("test"),
        turn_preview="preview",
    )

    result = await managed_thread_turns_module.complete_managed_thread_execution(
        coordinator,
        managed_thread_turns_module.ManagedThreadSubmissionResult(
            started_execution=started,
            queued=False,
        ),
        ensure_queue_worker=lambda: events.append("ensure"),
        direct_hooks=managed_thread_turns_module.ManagedThreadCoordinatorHooks(
            on_execution_started=lambda started_execution: events.append("started"),
            on_execution_finished=lambda started_execution: events.append("finished"),
        ),
        runtime_event_state=RuntimeThreadRunEventState(),
    )

    assert result.queued is False
    assert result.finalized == {"status": "ok", "managed_turn_id": "exec-1"}
    assert events == ["started", "finalize", "finished", "ensure"]


@pytest.mark.anyio
async def test_complete_managed_thread_execution_starts_queue_worker_for_queued_submission(
    tmp_path: Path,
) -> None:
    started = _build_started_execution(tmp_path)
    coordinator = managed_thread_turns_module.ManagedThreadTurnCoordinator(
        orchestration_service=SimpleNamespace(),
        state_root=tmp_path,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Test",
            surface_kind="test",
            surface_key="surface-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="public",
            timeout_error="timeout",
            interrupted_error="interrupted",
            timeout_seconds=30,
        ),
        logger=logging.getLogger("test"),
        turn_preview="preview",
    )
    events: list[str] = []

    result = await managed_thread_turns_module.complete_managed_thread_execution(
        coordinator,
        managed_thread_turns_module.ManagedThreadSubmissionResult(
            started_execution=started,
            queued=True,
        ),
        ensure_queue_worker=lambda: events.append("ensure"),
    )

    assert result.queued is True
    assert result.finalized is None
    assert events == ["ensure"]


def test_resolve_managed_thread_target_resumes_matching_binding(tmp_path: Path) -> None:
    canonical_workspace = str(tmp_path.resolve())
    thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="paused",
        backend_thread_id="old-thread",
        backend_runtime_instance_id="runtime-old",
    )
    binding = SimpleNamespace(thread_target_id="thread-1", mode="pma")
    resume_calls: list[dict[str, Any]] = []
    upserts: list[dict[str, Any]] = []

    class _Service:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            _ = surface_kind, surface_key
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return thread

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            resume_calls.append(kwargs)
            return SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                agent_profile=None,
                workspace_root=canonical_workspace,
                lifecycle_status="active",
                backend_thread_id=kwargs.get("backend_thread_id"),
                backend_runtime_instance_id=kwargs.get("backend_runtime_instance_id"),
            )

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create_thread_target should not be called")

        def upsert_binding(self, **kwargs: Any) -> None:
            upserts.append(kwargs)

    _, resolved_thread = managed_thread_turns_module.resolve_managed_thread_target(
        _Service(),
        request=managed_thread_turns_module.ManagedThreadTargetRequest(
            surface_kind="telegram",
            surface_key="telegram:-1001:101",
            mode="pma",
            agent="codex",
            workspace_root=tmp_path,
            display_name="telegram:surface",
            backend_thread_id="backend-new",
            backend_runtime_instance_id="runtime-new",
            binding_metadata={"topic_key": "telegram:-1001:101"},
        ),
    )

    assert resolved_thread is not None
    assert resume_calls == [
        {
            "backend_thread_id": "backend-new",
            "backend_runtime_instance_id": "runtime-new",
        }
    ]
    assert upserts == [
        {
            "surface_kind": "telegram",
            "surface_key": "telegram:-1001:101",
            "thread_target_id": "thread-1",
            "agent_id": "codex",
            "repo_id": None,
            "resource_kind": None,
            "resource_id": None,
            "mode": "pma",
            "metadata": {"topic_key": "telegram:-1001:101"},
        }
    ]
