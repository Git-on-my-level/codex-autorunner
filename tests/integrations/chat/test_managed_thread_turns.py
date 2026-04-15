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
from codex_autorunner.core.orchestration.runtime_threads import (
    RUNTIME_THREAD_TIMEOUT_ERROR,
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore


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


def test_render_managed_thread_response_text_prepends_session_notice() -> None:
    rendered = managed_thread_turns_module.render_managed_thread_response_text(
        managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="Recovered answer",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            backend_thread_id="backend-1",
            session_notice="Notice: a new session was started.",
        )
    )

    assert rendered == "Notice: a new session was started.\n\nRecovered answer"


@pytest.mark.anyio
async def test_managed_thread_turn_coordinator_runs_lifecycle_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    events: list[Any] = []

    async def _fake_finalize(
        **kwargs: Any,
    ) -> managed_thread_turns_module.ManagedThreadFinalizationResult:
        assert kwargs["started"] is started
        assert kwargs["runtime_event_state"] is not None
        assert kwargs["on_progress_event"] is progress_handler
        events.append("finalize")
        return managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="exec-1",
            backend_thread_id=None,
        )

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
        hooks=managed_thread_turns_module.ManagedThreadExecutionHooks(
            on_execution_started=_on_started,
            on_execution_finished=_on_finished,
            on_progress_event=progress_handler,
        ),
        runtime_event_state=RuntimeThreadRunEventState(),
    )

    assert result == managed_thread_turns_module.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="exec-1",
        backend_thread_id=None,
    )
    assert events == ["started", "finalize", "finished"]


@pytest.mark.anyio
async def test_managed_thread_turn_coordinator_uses_preview_builder_per_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _build_started_execution(tmp_path)
    captured_preview: dict[str, str] = {}

    async def _fake_finalize(
        **kwargs: Any,
    ) -> managed_thread_turns_module.ManagedThreadFinalizationResult:
        captured_preview["value"] = kwargs["turn_preview"]
        return managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="exec-1",
            backend_thread_id=None,
        )

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

    async def _fake_finalize(
        **kwargs: Any,
    ) -> managed_thread_turns_module.ManagedThreadFinalizationResult:
        assert kwargs["started"] is started
        events.append("finalize")
        return managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id=started.thread.thread_target_id,
            managed_turn_id=started.execution.execution_id,
            backend_thread_id=None,
        )

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

    async def _deliver_result(
        finalized: managed_thread_turns_module.ManagedThreadFinalizationResult,
    ) -> None:
        events.append(("deliver", finalized.managed_turn_id))

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
        spawn_task=lambda coro: (
            spawned_tasks.append(asyncio.create_task(coro)) or spawned_tasks[-1]
        ),
        hooks=managed_thread_turns_module.ManagedThreadQueueWorkerHooks(
            deliver_result=_deliver_result,
            run_with_indicator=_run_with_indicator,
            execution_hooks=managed_thread_turns_module.ManagedThreadExecutionHooks(
                on_execution_started=lambda started_execution: events.append("started"),
                on_execution_finished=lambda started_execution: events.append(
                    "finished"
                ),
            ),
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

    async def _fake_finalize(
        **kwargs: Any,
    ) -> managed_thread_turns_module.ManagedThreadFinalizationResult:
        started = kwargs["started"]
        if started.execution.execution_id == "exec-1":
            raise RuntimeError("worker finalize exploded")
        return managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id=started.thread.thread_target_id,
            managed_turn_id=started.execution.execution_id,
            backend_thread_id=None,
        )

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

    async def _deliver_result(
        finalized: managed_thread_turns_module.ManagedThreadFinalizationResult,
    ) -> None:
        delivered.append(
            (
                finalized.managed_turn_id,
                finalized.status,
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
        record_execution_result=lambda thread_id, execution_id, **kwargs: (
            recorded_errors.append(
                (thread_id, execution_id, str(kwargs.get("error") or ""))
            )
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
        spawn_task=lambda coro: (
            spawned_tasks.append(asyncio.create_task(coro)) or spawned_tasks[-1]
        ),
        hooks=managed_thread_turns_module.ManagedThreadQueueWorkerHooks(
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

    async def _fake_finalize(
        **kwargs: Any,
    ) -> managed_thread_turns_module.ManagedThreadFinalizationResult:
        assert kwargs["started"] is started
        events.append("finalize")
        return managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id=started.thread.thread_target_id,
            managed_turn_id=started.execution.execution_id,
            backend_thread_id=None,
        )

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
        direct_hooks=managed_thread_turns_module.ManagedThreadExecutionHooks(
            on_execution_started=lambda started_execution: events.append("started"),
            on_execution_finished=lambda started_execution: events.append("finished"),
        ),
        runtime_event_state=RuntimeThreadRunEventState(),
    )

    assert result.queued is False
    assert (
        result.finalized
        == managed_thread_turns_module.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="exec-1",
            backend_thread_id=None,
        )
    )
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


def test_resolve_managed_thread_target_clears_stale_backend_for_fresh_pma_session(
    tmp_path: Path,
) -> None:
    canonical_workspace = str(tmp_path.resolve())
    thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="paused",
        backend_thread_id="stale-session",
        backend_runtime_instance_id="runtime-old",
    )
    binding = SimpleNamespace(thread_target_id="thread-1", mode="pma")
    clear_calls: list[dict[str, Any]] = []
    resume_calls: list[dict[str, Any]] = []

    class _Service:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            _ = surface_kind, surface_key
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return thread

        def set_thread_backend_id(
            self,
            thread_target_id: str,
            backend_thread_id: Optional[str],
            *,
            backend_runtime_instance_id: Optional[str] = None,
        ) -> None:
            assert thread_target_id == "thread-1"
            clear_calls.append(
                {
                    "backend_thread_id": backend_thread_id,
                    "backend_runtime_instance_id": backend_runtime_instance_id,
                }
            )
            thread.backend_thread_id = backend_thread_id
            thread.backend_runtime_instance_id = backend_runtime_instance_id

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            resume_calls.append(kwargs)
            if kwargs.get("backend_thread_id") is not None:
                thread.backend_thread_id = kwargs.get("backend_thread_id")
            thread.backend_runtime_instance_id = kwargs.get(
                "backend_runtime_instance_id"
            )
            return SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                agent_profile=None,
                workspace_root=canonical_workspace,
                lifecycle_status="active",
                backend_thread_id=thread.backend_thread_id,
                backend_runtime_instance_id=thread.backend_runtime_instance_id,
            )

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create_thread_target should not be called")

        def upsert_binding(self, **kwargs: Any) -> None:
            pass

    _, resolved_thread = managed_thread_turns_module.resolve_managed_thread_target(
        _Service(),
        request=managed_thread_turns_module.ManagedThreadTargetRequest(
            surface_kind="telegram",
            surface_key="telegram:-1001:101",
            mode="pma",
            agent="codex",
            workspace_root=tmp_path,
            display_name="telegram:surface",
            binding_metadata={"topic_key": "telegram:-1001:101"},
        ),
    )

    assert resolved_thread is not None
    assert resume_calls == [
        {
            "backend_thread_id": None,
            "backend_runtime_instance_id": None,
        }
    ]
    assert clear_calls == [
        {
            "backend_thread_id": None,
            "backend_runtime_instance_id": None,
        }
    ]
    assert resolved_thread.backend_thread_id is None


def test_resolve_managed_thread_target_keeps_backend_for_repo_resume_without_rebind(
    tmp_path: Path,
) -> None:
    canonical_workspace = str(tmp_path.resolve())
    thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="archived",
        backend_thread_id="backend-existing",
        backend_runtime_instance_id=None,
    )
    binding = SimpleNamespace(thread_target_id="thread-1", mode="repo")
    clear_calls: list[dict[str, Any]] = []
    resume_calls: list[dict[str, Any]] = []

    class _Service:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            _ = surface_kind, surface_key
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return thread

        def set_thread_backend_id(
            self,
            thread_target_id: str,
            backend_thread_id: Optional[str],
            *,
            backend_runtime_instance_id: Optional[str] = None,
        ) -> None:
            assert thread_target_id == "thread-1"
            clear_calls.append(
                {
                    "backend_thread_id": backend_thread_id,
                    "backend_runtime_instance_id": backend_runtime_instance_id,
                }
            )

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            assert thread_target_id == "thread-1"
            resume_calls.append(kwargs)
            return SimpleNamespace(
                thread_target_id="thread-1",
                agent_id="codex",
                agent_profile=None,
                workspace_root=canonical_workspace,
                lifecycle_status="active",
                backend_thread_id=thread.backend_thread_id,
                backend_runtime_instance_id=thread.backend_runtime_instance_id,
            )

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create_thread_target should not be called")

        def upsert_binding(self, **kwargs: Any) -> None:
            pass

    _, resolved_thread = managed_thread_turns_module.resolve_managed_thread_target(
        _Service(),
        request=managed_thread_turns_module.ManagedThreadTargetRequest(
            surface_kind="discord",
            surface_key="discord:channel-1",
            mode="repo",
            agent="codex",
            workspace_root=tmp_path,
            display_name="discord:surface",
            binding_metadata={"channel_id": "channel-1"},
        ),
    )

    assert resolved_thread is not None
    assert clear_calls == []
    assert resume_calls == [
        {
            "backend_thread_id": "backend-existing",
            "backend_runtime_instance_id": None,
        }
    ]
    assert resolved_thread.backend_thread_id == "backend-existing"


def test_resolve_managed_thread_target_keeps_active_repo_binding_without_resume(
    tmp_path: Path,
) -> None:
    canonical_workspace = str(tmp_path.resolve())
    thread = SimpleNamespace(
        thread_target_id="thread-1",
        agent_id="codex",
        agent_profile=None,
        workspace_root=canonical_workspace,
        lifecycle_status="active",
        backend_thread_id="backend-existing",
        backend_runtime_instance_id="runtime-1",
    )
    binding = SimpleNamespace(thread_target_id="thread-1", mode="repo")
    resume_calls: list[dict[str, Any]] = []

    class _Service:
        def get_binding(self, *, surface_kind: str, surface_key: str) -> Any:
            _ = surface_kind, surface_key
            return binding

        def get_thread_target(self, thread_target_id: str) -> Any:
            assert thread_target_id == "thread-1"
            return thread

        def resume_thread_target(self, thread_target_id: str, **kwargs: Any) -> Any:
            _ = thread_target_id
            resume_calls.append(kwargs)
            raise AssertionError("resume_thread_target should not be called")

        def create_thread_target(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("create_thread_target should not be called")

        def upsert_binding(self, **kwargs: Any) -> None:
            _ = kwargs

    _, resolved_thread = managed_thread_turns_module.resolve_managed_thread_target(
        _Service(),
        request=managed_thread_turns_module.ManagedThreadTargetRequest(
            surface_kind="discord",
            surface_key="discord:channel-1",
            mode="repo",
            agent="codex",
            workspace_root=tmp_path,
            display_name="discord:surface",
            binding_metadata={"channel_id": "channel-1"},
        ),
    )

    assert resolved_thread is thread
    assert resume_calls == []


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


def _started_execution_with_backend_ids(tmp_path: Path) -> RuntimeThreadExecution:
    return RuntimeThreadExecution(
        service=SimpleNamespace(),
        harness=SimpleNamespace(),
        thread=ThreadTarget(
            thread_target_id="thread-1",
            agent_id="hermes",
            workspace_root=str(tmp_path),
            lifecycle_status="active",
            backend_thread_id="session-1",
        ),
        execution=ExecutionRecord(
            execution_id="exec-1",
            target_id="thread-1",
            target_kind="thread",
            status="running",
            backend_id="turn-1",
        ),
        workspace_root=tmp_path,
        request=MessageRequest(
            target_id="thread-1",
            target_kind="thread",
            message_text="hello",
        ),
    )


class _FakeThreadStore:
    def __init__(self, _root: Path) -> None:
        self.updated: list[tuple[str, str, str]] = []

    def get_thread(self, _managed_thread_id: str) -> dict[str, Any]:
        return {}

    def update_thread_after_turn(
        self,
        managed_thread_id: str,
        *,
        last_turn_id: str,
        last_message_preview: str,
    ) -> None:
        self.updated.append((managed_thread_id, last_turn_id, last_message_preview))


class _FakeTranscriptStore:
    def __init__(self, _root: Path) -> None:
        self.writes: list[tuple[str, dict[str, Any], str]] = []

    def write_transcript(
        self,
        *,
        turn_id: str,
        metadata: dict[str, Any],
        assistant_text: str,
    ) -> None:
        self.writes.append((turn_id, metadata, assistant_text))


class _FakeHubPersistenceClient:
    def __init__(self) -> None:
        self.timeline_requests: list[Any] = []
        self.trace_requests: list[Any] = []
        self.transcript_requests: list[Any] = []
        self.activity_requests: list[Any] = []

    async def persist_execution_timeline(self, request: Any) -> Any:
        self.timeline_requests.append(request)
        return SimpleNamespace(
            execution_id=request.execution_id,
            persisted_event_count=len(request.events),
        )

    async def finalize_execution_cold_trace(self, request: Any) -> Any:
        self.trace_requests.append(request)
        return SimpleNamespace(
            execution_id=request.execution_id,
            trace_manifest_id="trace-1",
        )

    async def write_transcript(self, request: Any) -> Any:
        self.transcript_requests.append(request)
        return SimpleNamespace(turn_id=request.turn_id)

    async def record_thread_activity(self, request: Any) -> None:
        self.activity_requests.append(request)


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_self_claims_existing_pr_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_store = PrBindingStore(tmp_path)
    thread_store = PmaThreadStore(tmp_path)
    created = thread_store.create_thread(
        "hermes",
        tmp_path.resolve(),
        repo_id="repo-1",
        metadata={"head_branch": "feature/self-claim"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    started = RuntimeThreadExecution(
        service=SimpleNamespace(),
        harness=SimpleNamespace(),
        thread=SimpleNamespace(
            thread_target_id=managed_thread_id,
            agent_id="hermes",
            workspace_root=str(tmp_path),
            lifecycle_status="active",
            backend_thread_id="session-1",
            metadata={"head_branch": "feature/self-claim"},
        ),
        execution=ExecutionRecord(
            execution_id="exec-1",
            target_id=managed_thread_id,
            target_kind="thread",
            status="running",
            backend_id="turn-1",
        ),
        workspace_root=tmp_path,
        request=MessageRequest(
            target_id=managed_thread_id,
            target_kind="thread",
            message_text="hello",
        ),
    )
    fake_hub_client = _FakeHubPersistenceClient()
    binding = binding_store.upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=17,
        pr_state="open",
        head_branch="feature/self-claim",
        base_branch="main",
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: False,
    )

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="Implemented changes and opened PR #17.",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1",
            repo_id="repo-1",
            resource_kind="repo",
            resource_id="repo-1",
            agent_id="hermes",
            workspace_root=str(tmp_path),
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        raw_config={
            "github": {
                "automation": {"polling": {"enabled": True, "interval_seconds": 90}}
            }
        },
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.self_claim"),
        turn_preview="preview",
    )

    claimed = PrBindingStore(tmp_path).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )
    watch = ScmPollingWatchStore(tmp_path).get_watch(
        provider="github",
        binding_id=binding.binding_id,
    )

    assert result.status == "ok"
    assert claimed is not None
    assert claimed.thread_target_id == managed_thread_id
    assert watch is not None
    assert watch.workspace_root == str(tmp_path.resolve())


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_logs_timeout_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: False,
    )

    async def _timeout_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=RUNTIME_THREAD_TIMEOUT_ERROR,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _timeout_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="error",
            error=kwargs.get("error"),
        ),
    )

    caplog.set_level(logging.INFO)
    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1:msg-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.timeout"),
        turn_preview="preview",
    )

    assert result.status == "error"
    assert "chat.managed_thread.turn_finalized" in caplog.text
    assert '"completion_source":"timeout"' in caplog.text
    assert '"managed_thread_id":"thread-1"' in caplog.text
    assert '"backend_thread_id":"session-1"' in caplog.text
    assert len(fake_hub_client.timeline_requests) == 1
    assert fake_hub_client.timeline_requests[0].metadata["status"] == "error"
    assert fake_hub_client.timeline_requests[0].metadata["backend_turn_id"] == "turn-1"
    assert (
        fake_hub_client.timeline_requests[0].metadata["trace_manifest_id"] == "trace-1"
    )
    assert len(fake_hub_client.trace_requests) == 1
    assert fake_hub_client.trace_requests[0].backend_turn_id == "turn-1"
    assert fake_hub_client.transcript_requests == []
    assert fake_hub_client.activity_requests == []


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_propagates_session_recovery_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    started.request.metadata["fresh_backend_session_started"] = True
    started.request.metadata["fresh_backend_session_reason"] = "missing_backend_binding"
    started.request.metadata["fresh_backend_session_notice"] = (
        "Notice: the previous live session was unavailable, so I started a new "
        "session and recovered context from durable history."
    )
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: False,
    )

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-2",
            backend_turn_id="turn-2",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-2"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-2"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Telegram",
            surface_kind="telegram",
            surface_key="telegram:-1001:101",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Telegram PMA execution failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.session_recovery"),
        turn_preview="preview",
    )

    assert result.status == "ok"
    assert (
        result.session_notice
        == "Notice: the previous live session was unavailable, so I started a new "
        "session and recovered context from durable history."
    )
    assert result.fresh_backend_session_reason == "missing_backend_binding"
    assert len(fake_hub_client.timeline_requests) == 1
    assert (
        fake_hub_client.timeline_requests[0].metadata["fresh_backend_session_started"]
        is True
    )
    assert (
        fake_hub_client.timeline_requests[0].metadata["fresh_backend_session_reason"]
        == "missing_backend_binding"
    )
    assert len(fake_hub_client.transcript_requests) == 1
    assert (
        fake_hub_client.transcript_requests[0].metadata["fresh_backend_session_started"]
        is True
    )
    assert (
        fake_hub_client.transcript_requests[0].metadata["fresh_backend_session_reason"]
        == "missing_backend_binding"
    )


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_logs_finalization_failure_after_prompt_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: False,
    )

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="error",
            error="persist failed",
        ),
        get_execution=lambda managed_thread_id, managed_turn_id: None,
    )

    caplog.set_level(logging.INFO)
    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Telegram",
            surface_kind="telegram",
            surface_key="telegram:-1001:101",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Telegram PMA execution failed",
            timeout_error="Telegram PMA turn timed out",
            interrupted_error="Telegram PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.finalization"),
        turn_preview="preview",
    )

    assert result.status == "error"
    assert "chat.managed_thread.finalization_failed_after_prompt_return" in caplog.text
    assert '"completion_source":"prompt_return"' in caplog.text
    assert '"managed_turn_id":"exec-1"' in caplog.text
    assert len(fake_hub_client.timeline_requests) == 1
    assert fake_hub_client.timeline_requests[0].metadata["status"] == "ok"
    assert fake_hub_client.timeline_requests[0].metadata["backend_turn_id"] == "turn-1"
    assert (
        fake_hub_client.timeline_requests[0].metadata["trace_manifest_id"] == "trace-1"
    )
    assert len(fake_hub_client.trace_requests) == 1
    assert fake_hub_client.trace_requests[0].backend_turn_id == "turn-1"
    assert len(fake_hub_client.transcript_requests) == 1
    assert fake_hub_client.transcript_requests[0].assistant_text == "fixture reply"
    assert fake_hub_client.activity_requests == []


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_continues_when_timeline_persistence_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: True,
    )

    async def _progress_stream(*args: Any, **kwargs: Any):
        _ = args, kwargs
        yield {"message": {"method": "session/update"}}

    async def _normalize(*args: Any, **kwargs: Any) -> list[Any]:
        _ = args, kwargs
        return [
            managed_thread_turns_module.RunNotice(
                timestamp="2026-04-15T00:00:00Z",
                kind="progress",
                message="partial",
            )
        ]

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    async def _cancelled_timeline_persist(*args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_progress_event_stream",
        _progress_stream,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_normalize_runtime_progress_event",
        _normalize,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_persist_execution_timeline_via_hub",
        _cancelled_timeline_persist,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    caplog.set_level(logging.INFO)
    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1:msg-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.timeline_cancelled"),
        turn_preview="preview",
    )

    assert result.status == "ok"
    assert "chat.managed_thread.turn_finalized" in caplog.text
    assert "thread timeline persistence cancelled" in caplog.text
    assert len(fake_hub_client.trace_requests) == 1
    assert len(fake_hub_client.transcript_requests) == 1
    assert len(fake_hub_client.activity_requests) == 1


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_batches_live_timeline_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: True,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_LIVE_TIMELINE_BATCH_MAX_EVENTS",
        3,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_LIVE_TIMELINE_BATCH_MAX_DELAY_SECONDS",
        60.0,
    )

    async def _progress_stream(*args: Any, **kwargs: Any):
        _ = args, kwargs
        for index in range(9):
            yield {
                "index": index,
                "message": {"method": "session/update"},
            }

    async def _normalize(raw_event: Any, _state: Any) -> list[Any]:
        index = int(raw_event["index"])
        return [
            managed_thread_turns_module.RunNotice(
                timestamp=f"2026-04-15T00:00:{index:02d}Z",
                kind="progress",
                message=f"partial-{index}",
            )
        ]

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_progress_event_stream",
        _progress_stream,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_normalize_runtime_progress_event",
        _normalize,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1:msg-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.timeline_batching"),
        turn_preview="preview",
    )

    assert result.status == "ok"
    assert len(fake_hub_client.timeline_requests) == 4
    assert [request.start_index for request in fake_hub_client.timeline_requests] == [
        1,
        4,
        7,
        10,
    ]
    assert [len(request.events) for request in fake_hub_client.timeline_requests] == [
        3,
        3,
        3,
        1,
    ]
    assert [
        request.metadata["status"] for request in fake_hub_client.timeline_requests
    ] == [
        "running",
        "running",
        "running",
        "ok",
    ]


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_flushes_live_timeline_on_delay_without_more_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buffered events must flush when the delay cap elapses, not only on the next chunk."""
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: True,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_LIVE_TIMELINE_BATCH_MAX_EVENTS",
        100,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_LIVE_TIMELINE_BATCH_MAX_DELAY_SECONDS",
        0.05,
    )

    async def _progress_stream(*args: Any, **kwargs: Any):
        _ = args, kwargs
        yield {"index": 0, "message": {"method": "session/update"}}
        await asyncio.sleep(0.15)
        yield {"index": 1, "message": {"method": "session/update"}}

    async def _normalize(raw_event: Any, _state: Any) -> list[Any]:
        index = int(raw_event["index"])
        return [
            managed_thread_turns_module.RunNotice(
                timestamp=f"2026-04-15T00:00:{index:02d}Z",
                kind="progress",
                message=f"partial-{index}",
            )
        ]

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_progress_event_stream",
        _progress_stream,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "_normalize_runtime_progress_event",
        _normalize,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1:msg-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.timeline_delay_flush"),
        turn_preview="preview",
    )

    assert result.status == "ok"
    running = [
        r
        for r in fake_hub_client.timeline_requests
        if r.metadata["status"] == "running"
    ]
    assert len(running) >= 2
    assert [len(r.events) for r in running] == [1, 1]


@pytest.mark.anyio
async def test_finalize_managed_thread_execution_continues_when_progress_pump_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = _started_execution_with_backend_ids(tmp_path)
    fake_hub_client = _FakeHubPersistenceClient()
    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_supports_progress_event_stream",
        lambda _harness: True,
    )

    async def _cancelled_stream(*args: Any, **kwargs: Any):
        _ = args, kwargs
        if False:
            yield {}
        raise asyncio.CancelledError()

    async def _successful_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="fixture reply",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

    monkeypatch.setattr(
        managed_thread_turns_module,
        "harness_progress_event_stream",
        _cancelled_stream,
    )
    monkeypatch.setattr(
        managed_thread_turns_module,
        "await_runtime_thread_outcome",
        _successful_outcome,
    )

    orchestration_service = SimpleNamespace(
        get_thread_target=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
            backend_thread_id="session-1"
        ),
        record_execution_result=lambda *args, **kwargs: SimpleNamespace(
            status="ok",
            error=None,
        ),
    )

    caplog.set_level(logging.INFO)
    result = await managed_thread_turns_module.finalize_managed_thread_execution(
        orchestration_service=orchestration_service,
        started=started,
        state_root=tmp_path,
        hub_client=fake_hub_client,
        surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key="discord:chan-1:msg-1",
        ),
        errors=managed_thread_turns_module.ManagedThreadErrorMessages(
            public_execution_error="Discord PMA execution failed",
            timeout_error="Discord PMA turn timed out",
            interrupted_error="Discord PMA turn interrupted",
            timeout_seconds=5,
        ),
        logger=logging.getLogger("test.managed_thread.pump_cancelled"),
        turn_preview="preview",
    )

    assert result.status == "ok"
    assert "chat.managed_thread.turn_finalized" in caplog.text
    assert "progress event pump cancelled during finalization" in caplog.text
    assert len(fake_hub_client.timeline_requests) == 1
    assert len(fake_hub_client.trace_requests) == 1


class TestPostCompletionRecoveryCharacterization:
    @pytest.mark.anyio
    async def test_recover_post_completion_outcome_upgrades_error_to_ok_when_completed_seen(
        self,
    ) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_events import (
            recover_post_completion_outcome,
        )

        event_state = RuntimeThreadRunEventState()
        event_state.completed_seen = True
        event_state.note_message_text("Recovered answer from stream")

        error_outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="transport disconnect",
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

        recovered = recover_post_completion_outcome(error_outcome, event_state)
        assert recovered.status == "ok"
        assert recovered.assistant_text == "Recovered answer from stream"
        assert recovered.error is None
        assert recovered.backend_thread_id == "session-1"
        assert recovered.backend_turn_id == "turn-1"

    @pytest.mark.anyio
    async def test_recover_post_completion_outcome_does_not_upgrade_without_completed_seen(
        self,
    ) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_events import (
            recover_post_completion_outcome,
        )

        event_state = RuntimeThreadRunEventState()
        event_state.completed_seen = False
        event_state.note_message_text("Some streamed text")

        error_outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="transport disconnect",
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

        recovered = recover_post_completion_outcome(error_outcome, event_state)
        assert recovered is error_outcome

    @pytest.mark.anyio
    async def test_recover_post_completion_outcome_does_not_upgrade_without_assistant_text(
        self,
    ) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_events import (
            recover_post_completion_outcome,
        )

        event_state = RuntimeThreadRunEventState()
        event_state.completed_seen = True

        error_outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error="transport disconnect",
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

        recovered = recover_post_completion_outcome(error_outcome, event_state)
        assert recovered is error_outcome

    @pytest.mark.anyio
    async def test_recover_post_completion_outcome_passes_ok_outcome_through_unchanged(
        self,
    ) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_events import (
            recover_post_completion_outcome,
        )

        event_state = RuntimeThreadRunEventState()
        event_state.completed_seen = True
        event_state.note_message_text("Streamed text")

        ok_outcome = RuntimeThreadOutcome(
            status="ok",
            assistant_text="Direct answer",
            error=None,
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

        recovered = recover_post_completion_outcome(ok_outcome, event_state)
        assert recovered is ok_outcome

    @pytest.mark.anyio
    async def test_recover_post_completion_outcome_upgrades_interrupted_to_ok(
        self,
    ) -> None:
        from codex_autorunner.core.orchestration.runtime_thread_events import (
            recover_post_completion_outcome,
        )

        event_state = RuntimeThreadRunEventState()
        event_state.completed_seen = True
        event_state.note_message_text("Interrupted but complete")

        interrupted_outcome = RuntimeThreadOutcome(
            status="interrupted",
            assistant_text="",
            error="Runtime thread interrupted",
            backend_thread_id="session-1",
            backend_turn_id="turn-1",
        )

        recovered = recover_post_completion_outcome(interrupted_outcome, event_state)
        assert recovered.status == "ok"
        assert recovered.assistant_text == "Interrupted but complete"


class TestBackendTurnIdFallbackCharacterization:
    @pytest.mark.anyio
    async def test_finalize_falls_back_to_execution_id_when_backend_id_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        started = RuntimeThreadExecution(
            service=SimpleNamespace(),
            harness=SimpleNamespace(),
            thread=ThreadTarget(
                thread_target_id="thread-1",
                agent_id="codex",
                workspace_root=str(tmp_path),
                lifecycle_status="active",
                backend_thread_id="session-1",
            ),
            execution=ExecutionRecord(
                execution_id="exec-no-backend",
                target_id="thread-1",
                target_kind="thread",
                status="running",
                backend_id=None,
            ),
            workspace_root=tmp_path,
            request=MessageRequest(
                target_id="thread-1",
                target_kind="thread",
                message_text="hello",
            ),
        )
        fake_hub_client = _FakeHubPersistenceClient()
        monkeypatch.setattr(
            managed_thread_turns_module,
            "harness_supports_progress_event_stream",
            lambda _harness: False,
        )

        async def _ok_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
            return RuntimeThreadOutcome(
                status="ok",
                assistant_text="done",
                error=None,
                backend_thread_id="session-1",
                backend_turn_id="turn-ok",
            )

        monkeypatch.setattr(
            managed_thread_turns_module,
            "await_runtime_thread_outcome",
            _ok_outcome,
        )

        orchestration_service = SimpleNamespace(
            get_thread_target=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            record_execution_result=lambda *args, **kwargs: SimpleNamespace(
                status="ok",
                error=None,
            ),
        )

        caplog.set_level(logging.WARNING)
        result = await managed_thread_turns_module.finalize_managed_thread_execution(
            orchestration_service=orchestration_service,
            started=started,
            state_root=tmp_path,
            hub_client=fake_hub_client,
            surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                log_label="Test",
                surface_kind="test",
                surface_key="test:surface-1",
            ),
            errors=managed_thread_turns_module.ManagedThreadErrorMessages(
                public_execution_error="failed",
                timeout_error="timeout",
                interrupted_error="interrupted",
                timeout_seconds=5,
            ),
            logger=logging.getLogger("test.backend_fallback"),
            turn_preview="preview",
        )

        assert result.status == "ok"
        assert "backend_turn_id_fallback" in caplog.text
        assert "exec-no-backend" in caplog.text


class TestFinalizationSideEffectsCharacterization:
    @pytest.mark.anyio
    async def test_finalize_writes_transcript_on_ok_but_not_on_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started_ok = _started_execution_with_backend_ids(tmp_path)
        started_err = _replace_started_execution(
            started_ok,
            execution_id="exec-err",
            message_text="error turn",
        )
        fake_hub_client = _FakeHubPersistenceClient()
        monkeypatch.setattr(
            managed_thread_turns_module,
            "harness_supports_progress_event_stream",
            lambda _harness: False,
        )

        async def _ok_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
            return RuntimeThreadOutcome(
                status="ok",
                assistant_text="ok reply",
                error=None,
                backend_thread_id="session-1",
                backend_turn_id="turn-1",
            )

        async def _err_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
            return RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error="something failed",
                backend_thread_id="session-1",
                backend_turn_id="turn-err",
            )

        orchestration_service = SimpleNamespace(
            get_thread_target=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            record_execution_result=lambda *args, **kwargs: SimpleNamespace(
                status=kwargs.get("status", "ok"),
                error=kwargs.get("error"),
            ),
        )

        monkeypatch.setattr(
            managed_thread_turns_module,
            "await_runtime_thread_outcome",
            _ok_outcome,
        )
        result_ok = await managed_thread_turns_module.finalize_managed_thread_execution(
            orchestration_service=orchestration_service,
            started=started_ok,
            state_root=tmp_path,
            hub_client=fake_hub_client,
            surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                log_label="Test",
                surface_kind="test",
                surface_key="test:s-1",
            ),
            errors=managed_thread_turns_module.ManagedThreadErrorMessages(
                public_execution_error="failed",
                timeout_error="timeout",
                interrupted_error="interrupted",
                timeout_seconds=5,
            ),
            logger=logging.getLogger("test.transcript_ok"),
            turn_preview="preview",
        )
        assert result_ok.status == "ok"
        assert len(fake_hub_client.transcript_requests) == 1
        assert fake_hub_client.transcript_requests[0].assistant_text == "ok reply"

        fake_hub_client.transcript_requests.clear()
        monkeypatch.setattr(
            managed_thread_turns_module,
            "await_runtime_thread_outcome",
            _err_outcome,
        )
        result_err = (
            await managed_thread_turns_module.finalize_managed_thread_execution(
                orchestration_service=orchestration_service,
                started=started_err,
                state_root=tmp_path,
                hub_client=fake_hub_client,
                surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                    log_label="Test",
                    surface_kind="test",
                    surface_key="test:s-1",
                ),
                errors=managed_thread_turns_module.ManagedThreadErrorMessages(
                    public_execution_error="failed",
                    timeout_error="timeout",
                    interrupted_error="interrupted",
                    timeout_seconds=5,
                ),
                logger=logging.getLogger("test.transcript_err"),
                turn_preview="preview",
            )
        )
        assert result_err.status == "error"
        assert len(fake_hub_client.transcript_requests) == 0

    @pytest.mark.anyio
    async def test_finalize_writes_activity_on_ok_outcome(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = _started_execution_with_backend_ids(tmp_path)
        fake_hub_client = _FakeHubPersistenceClient()
        monkeypatch.setattr(
            managed_thread_turns_module,
            "harness_supports_progress_event_stream",
            lambda _harness: False,
        )

        async def _ok_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
            return RuntimeThreadOutcome(
                status="ok",
                assistant_text="activity test",
                error=None,
                backend_thread_id="session-1",
                backend_turn_id="turn-1",
            )

        monkeypatch.setattr(
            managed_thread_turns_module,
            "await_runtime_thread_outcome",
            _ok_outcome,
        )

        orchestration_service = SimpleNamespace(
            get_thread_target=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            record_execution_result=lambda *args, **kwargs: SimpleNamespace(
                status="ok",
                error=None,
            ),
        )

        result = await managed_thread_turns_module.finalize_managed_thread_execution(
            orchestration_service=orchestration_service,
            started=started,
            state_root=tmp_path,
            hub_client=fake_hub_client,
            surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                log_label="Test",
                surface_kind="test",
                surface_key="test:s-1",
            ),
            errors=managed_thread_turns_module.ManagedThreadErrorMessages(
                public_execution_error="failed",
                timeout_error="timeout",
                interrupted_error="interrupted",
                timeout_seconds=5,
            ),
            logger=logging.getLogger("test.activity_ok"),
            turn_preview="preview",
        )

        assert result.status == "ok"
        assert len(fake_hub_client.activity_requests) == 1

    @pytest.mark.anyio
    async def test_finalize_does_not_write_activity_on_error_outcome(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = _started_execution_with_backend_ids(tmp_path)
        fake_hub_client = _FakeHubPersistenceClient()
        monkeypatch.setattr(
            managed_thread_turns_module,
            "harness_supports_progress_event_stream",
            lambda _harness: False,
        )

        async def _err_outcome(*args: Any, **kwargs: Any) -> RuntimeThreadOutcome:
            return RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error="broke",
                backend_thread_id="session-1",
                backend_turn_id="turn-1",
            )

        monkeypatch.setattr(
            managed_thread_turns_module,
            "await_runtime_thread_outcome",
            _err_outcome,
        )

        orchestration_service = SimpleNamespace(
            get_thread_target=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            get_thread_runtime_binding=lambda managed_thread_id: SimpleNamespace(
                backend_thread_id="session-1"
            ),
            record_execution_result=lambda *args, **kwargs: SimpleNamespace(
                status="error",
                error="broke",
            ),
        )

        result = await managed_thread_turns_module.finalize_managed_thread_execution(
            orchestration_service=orchestration_service,
            started=started,
            state_root=tmp_path,
            hub_client=fake_hub_client,
            surface=managed_thread_turns_module.ManagedThreadSurfaceInfo(
                log_label="Test",
                surface_kind="test",
                surface_key="test:s-1",
            ),
            errors=managed_thread_turns_module.ManagedThreadErrorMessages(
                public_execution_error="failed",
                timeout_error="timeout",
                interrupted_error="interrupted",
                timeout_seconds=5,
            ),
            logger=logging.getLogger("test.activity_err"),
            turn_preview="preview",
        )

        assert result.status == "error"
        assert len(fake_hub_client.activity_requests) == 0
