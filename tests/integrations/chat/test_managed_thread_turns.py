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


def _replace_started_execution(
    started: RuntimeThreadExecution,
    *,
    execution_id: str,
    message_text: str,
) -> RuntimeThreadExecution:
    return RuntimeThreadExecution(
        service=started.service,
        harness=started.harness,
        thread=started.thread,
        execution=ExecutionRecord(
            execution_id=execution_id,
            target_id=started.execution.target_id,
            target_kind=started.execution.target_kind,
            status=started.execution.status,
        ),
        workspace_root=started.workspace_root,
        request=MessageRequest(
            target_id=started.request.target_id,
            target_kind=started.request.target_kind,
            message_text=message_text,
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
async def test_managed_thread_turn_coordinator_uses_preview_builder_per_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    captured_preview: dict[str, str] = {}

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        captured_preview["value"] = kwargs["turn_preview"]
        return {"status": "ok", "managed_turn_id": "exec-1"}

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
        turn_preview="stale-preview",
        preview_builder=lambda message_text: f"preview:{message_text}",
    )

    await coordinator.run_started_execution(started)

    assert captured_preview == {"value": "preview:hello"}


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
        "indicator:end",
        ("deliver", "exec-1"),
    ]
    assert task_map == {}


@pytest.mark.anyio
async def test_managed_thread_turn_coordinator_queue_worker_recovers_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build_started_execution(tmp_path)
    second = _replace_started_execution(
        first,
        execution_id="exec-2",
        message_text="second queued message",
    )
    begin_calls = 0
    delivered: list[tuple[str, str]] = []
    recorded_errors: list[tuple[str, str, str]] = []

    async def _fake_finalize(**kwargs: Any) -> dict[str, Any]:
        started = kwargs["started"]
        if started.execution.execution_id == "exec-1":
            raise RuntimeError("worker finalize exploded")
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
        begin_calls += 1
        if begin_calls == 1:
            return first
        if begin_calls == 2:
            return second
        return None

    async def _deliver_result(finalized: dict[str, Any]) -> None:
        delivered.append(
            (
                str(finalized["managed_turn_id"]),
                str(finalized["status"]),
            )
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "finalize_managed_thread_execution",
        _fake_finalize,
    )

    task_map: dict[str, asyncio.Task[Any]] = {}
    spawned_tasks: list[asyncio.Task[Any]] = []
    orchestration_service = SimpleNamespace(
        get_running_execution=lambda managed_thread_id: None,
        record_execution_result=lambda thread_id, execution_id, **kwargs: recorded_errors.append(
            (thread_id, execution_id, str(kwargs.get("error") or ""))
        ),
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
            deliver_result=_deliver_result,
        ),
        begin_next_execution=_fake_begin_next,
    )

    await asyncio.gather(*spawned_tasks)

    assert recorded_errors == [
        ("thread-1", "exec-1", "worker finalize exploded"),
    ]
    assert delivered == [("exec-1", "error"), ("exec-2", "ok")]
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


def test_resolve_managed_thread_target_reuses_backend_matched_thread(
    tmp_path: Path,
) -> None:
    canonical_workspace = str(tmp_path.resolve())
    binding = SimpleNamespace(thread_target_id="thread-current", mode="repo")
    current_thread = SimpleNamespace(
        thread_target_id="thread-current",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="active",
        backend_thread_id="backend-current",
    )
    archived_thread = SimpleNamespace(
        thread_target_id="thread-archived",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="archived",
        backend_thread_id="backend-resume",
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
    )
    resume_calls: list[tuple[str, dict[str, Any]]] = []
    upserts: list[dict[str, Any]] = []

    class _Service:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            _ = surface_kind, surface_key
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-current"
            return current_thread

        def list_thread_targets(self, **kwargs: Any) -> list[Any]:
            assert kwargs == {
                "agent_id": "codex",
                "repo_id": "repo-1",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "limit": 500,
            }
            return [archived_thread]

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            resume_calls.append((thread_target_id, kwargs))
            return SimpleNamespace(
                thread_target_id=thread_target_id,
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
            mode="repo",
            agent="codex",
            workspace_root=tmp_path,
            display_name="telegram:surface",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            backend_thread_id="backend-resume",
            binding_metadata={"topic_key": "telegram:-1001:101"},
        ),
    )

    assert resolved_thread is not None
    assert resume_calls == [
        (
            "thread-archived",
            {
                "backend_thread_id": "backend-resume",
                "backend_runtime_instance_id": None,
            },
        )
    ]
    assert upserts == [
        {
            "surface_kind": "telegram",
            "surface_key": "telegram:-1001:101",
            "thread_target_id": "thread-archived",
            "agent_id": "codex",
            "repo_id": "repo-1",
            "resource_kind": "repo",
            "resource_id": "repo-1",
            "mode": "repo",
            "metadata": {"topic_key": "telegram:-1001:101"},
        }
    ]
