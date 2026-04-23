from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from tests.support.web_test_helpers import build_pma_hub_app

from codex_autorunner.core.orchestration import (
    ColdTraceStore,
    OrchestrationBindingStore,
)
from codex_autorunner.core.orchestration.execution_history import ExecutionCheckpoint
from codex_autorunner.core.orchestration.runtime_bindings import (
    clear_runtime_thread_binding,
)
from codex_autorunner.core.orchestration.runtime_threads import RuntimeThreadOutcome
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_context import format_pma_discoverability_preamble
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.integrations.github import (
    managed_thread_pr_binding as managed_thread_pr_binding_module,
)
from codex_autorunner.surfaces.web.routes.pma_routes import managed_thread_runtime

pytestmark = pytest.mark.slow


def _patch_outcome_driven_finalization(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome_builder: Any,
) -> None:
    async def _fake_run_started_execution(
        self: Any,
        started: Any,
        *,
        hooks: Any = None,
        runtime_event_state: Any = None,
    ) -> Any:
        on_started = getattr(hooks, "on_execution_started", None)
        on_finalized = getattr(hooks, "on_execution_finalized", None)
        on_error = getattr(hooks, "on_execution_error", None)
        on_finished = getattr(hooks, "on_execution_finished", None)
        if callable(on_started):
            await on_started(started)
        try:
            outcome = await outcome_builder(
                orchestration_service=self.orchestration_service,
                started=started,
                state_root=self.state_root,
                hub_client=self.hub_client,
                surface=self.surface,
                errors=self.errors,
                logger=self.logger,
                turn_preview=self.turn_preview,
                raw_config=self.raw_config,
                runtime_event_state=runtime_event_state,
                on_progress_event=getattr(hooks, "on_progress_event", None),
            )
            orchestration_service = self.orchestration_service
            managed_thread_id = str(
                getattr(started.thread, "thread_target_id", None)
                or getattr(started.request, "target_id", None)
                or ""
            )
            managed_turn_id = str(getattr(started.execution, "execution_id", "") or "")
            if outcome.status == "ok":
                if callable(
                    getattr(orchestration_service, "record_execution_result", None)
                ):
                    orchestration_service.record_execution_result(
                        managed_thread_id,
                        managed_turn_id,
                        status="ok",
                        assistant_text=outcome.assistant_text,
                        error=None,
                        backend_turn_id=outcome.backend_turn_id,
                        transcript_turn_id=managed_turn_id,
                    )
            elif outcome.status == "interrupted":
                recorder = getattr(
                    orchestration_service, "record_execution_interrupted", None
                )
                if callable(recorder):
                    recorder(managed_thread_id, managed_turn_id)
            elif callable(
                getattr(orchestration_service, "record_execution_result", None)
            ):
                orchestration_service.record_execution_result(
                    managed_thread_id,
                    managed_turn_id,
                    status="error",
                    assistant_text="",
                    error=outcome.error,
                    backend_turn_id=outcome.backend_turn_id,
                    transcript_turn_id=None,
                )
            finalized = managed_thread_runtime.ManagedThreadFinalizationResult(
                status=outcome.status,
                assistant_text=outcome.assistant_text if outcome.status == "ok" else "",
                error=outcome.error,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                backend_thread_id=(
                    outcome.backend_thread_id
                    or getattr(started.thread, "backend_thread_id", None)
                ),
            )
            if callable(on_finalized):
                await on_finalized(started, finalized)
            return finalized
        except BaseException as exc:
            if callable(on_error):
                await on_error(started, exc)
            raise
        finally:
            if callable(on_finished):
                await on_finished(started)

    monkeypatch.setattr(
        managed_thread_runtime.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )


def test_finalized_with_backend_thread_fallback_prefers_finalized_value() -> None:
    finalized = managed_thread_runtime.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="done",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        backend_thread_id="backend-finalized",
    )
    started = SimpleNamespace(
        thread=SimpleNamespace(backend_thread_id="backend-started")
    )

    result = managed_thread_runtime._finalized_with_backend_thread_fallback(
        finalized,
        started=started,
        fallback_backend_thread_id="backend-fallback",
    )

    assert result is finalized


def test_finalized_with_backend_thread_fallback_uses_started_then_fallback() -> None:
    finalized = managed_thread_runtime.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="done",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        backend_thread_id=None,
    )

    started_result = managed_thread_runtime._finalized_with_backend_thread_fallback(
        finalized,
        started=SimpleNamespace(
            thread=SimpleNamespace(backend_thread_id="backend-started")
        ),
        fallback_backend_thread_id="backend-fallback",
    )
    fallback_result = managed_thread_runtime._finalized_with_backend_thread_fallback(
        finalized,
        started=SimpleNamespace(thread=SimpleNamespace(backend_thread_id=None)),
        fallback_backend_thread_id="backend-fallback",
    )

    assert started_result.backend_thread_id == "backend-started"
    assert fallback_result.backend_thread_id == "backend-fallback"


@pytest.mark.anyio
async def test_pma_queue_finalizer_returns_managed_thread_finalization_result(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    request = managed_thread_runtime._managed_thread_request_for_app(app)

    async def _fake_run_started_execution(
        self: Any,
        started: Any,
        *,
        hooks: Any = None,
        runtime_event_state: Any = None,
    ) -> Any:
        _ = self, started, hooks, runtime_event_state
        return managed_thread_runtime.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            managed_thread_id="thread-1",
            managed_turn_id="turn-1",
            backend_thread_id=None,
        )

    monkeypatch.setattr(
        managed_thread_runtime.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    finalized = await managed_thread_runtime._finalize_managed_thread_execution(
        request,
        service=SimpleNamespace(),
        thread_store=SimpleNamespace(),
        thread={"managed_thread_id": "thread-1"},
        started=SimpleNamespace(
            request=SimpleNamespace(message_text="hello"),
            thread=SimpleNamespace(
                thread_target_id="thread-1",
                backend_thread_id=None,
            ),
        ),
        fallback_backend_thread_id="backend-fallback",
    )

    assert isinstance(
        finalized,
        managed_thread_runtime.ManagedThreadFinalizationResult,
    )
    assert finalized.backend_thread_id == "backend-fallback"


@pytest.mark.anyio
async def test_restart_managed_thread_queue_workers_restores_pending_threads(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    store.create_turn(managed_thread_id, prompt="running")
    store.create_turn(managed_thread_id, prompt="queued", busy_policy="queue")

    restored: list[str] = []

    def _fake_ensure(app_obj: Any, thread_target_id: str) -> None:
        assert app_obj is app
        restored.append(thread_target_id)

    monkeypatch.setattr(
        managed_thread_runtime,
        "ensure_managed_thread_queue_worker",
        _fake_ensure,
    )

    await managed_thread_runtime.restart_managed_thread_queue_workers(app)

    assert restored == [managed_thread_id]


@pytest.mark.anyio
async def test_recover_orphaned_managed_thread_executions_unblocks_restart_queue(
    hub_env,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    running = store.create_turn(managed_thread_id, prompt="running")
    queued = store.create_turn(managed_thread_id, prompt="queued", busy_policy="queue")
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)
    with open_orchestration_sqlite(hub_env.hub_root) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET runtime_status = 'idle',
                       status_turn_id = NULL
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            )

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(app)

    updated_running = store.get_turn(managed_thread_id, running["managed_turn_id"])
    updated_queued = store.get_turn(managed_thread_id, queued["managed_turn_id"])
    assert updated_running is not None
    assert updated_running["status"] == "error"
    assert (
        updated_running["error"]
        == "Running execution could not be reattached after restart"
    )
    assert updated_queued is not None
    assert updated_queued["status"] == "queued"

    claimed = store.claim_next_queued_turn(managed_thread_id)
    assert claimed is not None
    claimed_turn, _queue_payload = claimed
    assert claimed_turn["managed_turn_id"] == queued["managed_turn_id"]


@pytest.mark.anyio
async def test_recover_orphaned_managed_thread_executions_restores_backend_ids_from_checkpoint(
    hub_env,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    running = store.create_turn(managed_thread_id, prompt="running")
    store.set_thread_backend_id(managed_thread_id, None)
    store.set_turn_backend_turn_id(running["managed_turn_id"], None)
    ColdTraceStore(hub_env.hub_root).save_checkpoint(
        ExecutionCheckpoint(
            execution_id=running["managed_turn_id"],
            thread_target_id=managed_thread_id,
            status="running",
            backend_thread_id="checkpoint-thread-1",
            backend_turn_id="checkpoint-turn-1",
            last_progress_at="2026-04-12T00:00:00Z",
        )
    )

    async def _has_turn(thread_id: str, turn_id: str) -> bool:
        return thread_id == "checkpoint-thread-1" and turn_id == "checkpoint-turn-1"

    app.state.app_server_events = SimpleNamespace(has_turn=_has_turn)

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(app)

    updated_running = store.get_turn(managed_thread_id, running["managed_turn_id"])
    updated_binding = store.get_thread_runtime_binding(managed_thread_id)
    assert updated_running is not None
    assert updated_running["status"] == "running"
    assert updated_running["backend_turn_id"] == "checkpoint-turn-1"
    assert updated_binding is not None
    assert updated_binding.backend_thread_id == "checkpoint-thread-1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("surface_kind", "surface_key", "client_turn_id"),
    (
        ("discord", "channel-123", "discord:channel-123:run-1"),
        ("telegram", "123:456", "telegram:123:456:run-1"),
    ),
)
async def test_recover_orphaned_managed_thread_executions_skips_chat_bound_threads(
    hub_env,
    surface_kind: str,
    surface_key: str,
    client_turn_id: str,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    bindings = OrchestrationBindingStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    running = store.create_turn(
        managed_thread_id,
        prompt="running",
        client_turn_id=client_turn_id,
    )
    bindings.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)
    with open_orchestration_sqlite(hub_env.hub_root) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET runtime_status = 'idle',
                       status_turn_id = NULL
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            )

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(app)

    updated_running = store.get_turn(managed_thread_id, running["managed_turn_id"])
    assert updated_running is not None
    assert updated_running["status"] == "running"
    assert updated_running["error"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("surface_kind", "surface_key"),
    (
        ("discord", "channel-123"),
        ("telegram", "123:456"),
    ),
)
async def test_recover_orphaned_managed_thread_executions_skips_chat_bound_threads_with_metadata(
    hub_env,
    surface_kind: str,
    surface_key: str,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    bindings = OrchestrationBindingStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    running = store.create_turn(
        managed_thread_id,
        prompt="running",
        metadata={
            "bound_chat_execution": {
                "origin": {
                    "kind": "surface",
                    "surface_kind": surface_kind,
                    "surface_key": surface_key,
                },
                "progress_targets": [
                    {
                        "surface_kind": surface_kind,
                        "surface_key": surface_key,
                    }
                ],
            }
        },
    )
    bindings.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)
    with open_orchestration_sqlite(hub_env.hub_root) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET runtime_status = 'idle',
                       status_turn_id = NULL
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            )

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(app)

    updated_running = store.get_turn(managed_thread_id, running["managed_turn_id"])
    assert updated_running is not None
    assert updated_running["status"] == "running"
    assert updated_running["error"] is None


def test_managed_thread_list_route_filters_by_status(hub_env) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)

    active = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    archived = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    store.archive_thread(str(archived["managed_thread_id"]))

    with TestClient(app) as client:
        active_resp = client.get("/hub/pma/threads", params={"status": "active"})
        archived_resp = client.get("/hub/pma/threads", params={"status": "archived"})

    assert active_resp.status_code == 200
    assert archived_resp.status_code == 200
    assert [item["managed_thread_id"] for item in active_resp.json()["threads"]] == [
        active["managed_thread_id"]
    ]
    assert [item["managed_thread_id"] for item in archived_resp.json()["threads"]] == [
        archived["managed_thread_id"]
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("surface_kind", "surface_key"),
    (
        ("discord", "channel-123"),
        ("telegram", "123:456"),
    ),
)
async def test_recover_orphaned_managed_thread_executions_recovers_pma_runs_on_chat_bound_threads(
    hub_env,
    surface_kind: str,
    surface_key: str,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    bindings = OrchestrationBindingStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    running = store.create_turn(managed_thread_id, prompt="running")
    queued = store.create_turn(managed_thread_id, prompt="queued", busy_policy="queue")
    bindings.upsert_binding(
        surface_kind=surface_kind,
        surface_key=surface_key,
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    clear_runtime_thread_binding(hub_env.hub_root, managed_thread_id)
    with open_orchestration_sqlite(hub_env.hub_root) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET runtime_status = 'idle',
                       status_turn_id = NULL
                 WHERE thread_target_id = ?
                """,
                (managed_thread_id,),
            )
    recovered: list[str] = []

    async def _fake_recover_bound_progress_execution(
        app_arg,
        *,
        service,
        thread_store,
        managed_thread_id: str,
        thread,
        execution,
    ) -> bool:
        _ = app_arg, service, thread_store, thread, execution
        recovered.append(managed_thread_id)
        return True

    monkeypatch.setattr(
        managed_thread_runtime,
        "_recover_pma_bound_chat_execution",
        _fake_recover_bound_progress_execution,
    )

    await managed_thread_runtime.recover_orphaned_managed_thread_executions(app)

    updated_running = store.get_turn(managed_thread_id, running["managed_turn_id"])
    updated_queued = store.get_turn(managed_thread_id, queued["managed_turn_id"])
    assert updated_running is not None
    assert updated_running["status"] == "running"
    assert updated_running["error"] is None
    assert updated_queued is not None
    assert updated_queued["status"] == "queued"
    assert recovered == [managed_thread_id]


def test_managed_thread_message_route_uses_orchestration_service_seam(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def __init__(self) -> None:
            self.record_calls: list[dict[str, Any]] = []

        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            self.record_calls.append(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                    "status": status,
                    "assistant_text": assistant_text,
                    "backend_turn_id": backend_turn_id,
                    "transcript_turn_id": transcript_turn_id,
                }
            )
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    fake_service = FakeService()

    def _fake_build_service(request, *, thread_store=None):
        _ = request, thread_store
        return fake_service

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        captured["service"] = service
        captured["request"] = request
        captured["sandbox_policy"] = sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        _fake_build_service,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["assistant_text"] == "assistant-output"
    assert payload["delivered_message"] == "hello from route"
    assert captured["request"].approval_mode == "never"
    assert captured["sandbox_policy"] == "dangerFullAccess"
    assert captured["request"].context_profile == "car_ambient"
    assert "<injected context>" not in captured["request"].metadata["runtime_prompt"]
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .startswith(format_pma_discoverability_preamble(hub_root=hub_env.hub_root))
    )
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .endswith("hello from route\n</user_message>\n")
    )
    assert fake_service.record_calls[0]["status"] == "ok"
    assert fake_service.record_calls[0]["execution_id"] == "managed-turn-1"


def test_managed_thread_message_route_uses_bound_progress_controller_for_direct_execution(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    call_order: list[str] = []
    controller_kwargs: dict[str, Any] = {}

    class FakeController:
        def __init__(self) -> None:
            self.hooks = SimpleNamespace(
                on_execution_started=self._on_started,
                on_execution_finished=self._on_finished,
                on_execution_finalized=self._on_finalized,
                on_execution_error=self._on_error,
                on_progress_event=None,
            )

        async def _on_started(self, started: Any) -> None:
            _ = started
            call_order.append("progress:start")

        async def _on_finished(self, started: Any) -> None:
            _ = started
            call_order.append("progress:finished")

        async def _on_finalized(self, started: Any, finalized: Any) -> None:
            _ = started
            call_order.append(f"progress:finalized:{finalized.status}")

        async def _on_error(self, started: Any, exc: BaseException) -> None:
            _ = started, exc
            call_order.append("progress:error")

        def surface_targets_for(
            self, managed_turn_id: str
        ) -> tuple[tuple[str, str], ...]:
            call_order.append(f"progress:targets:{managed_turn_id}")
            return ()

        def clear_surface_targets(self, managed_turn_id: str) -> None:
            call_order.append(f"progress:clear:{managed_turn_id}")

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
                thread_target_id=managed_thread_id,
                agent_id="codex",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
            harness=None,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    async def _fake_deliver(*args, **kwargs):
        _ = args, kwargs
        call_order.append("deliver:assistant")
        return SimpleNamespace(target_count=1, published_count=1, covered_targets=())

    async def _fake_notify(*args, **kwargs):
        _ = args, kwargs
        call_order.append("notify:terminal")

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "build_bound_chat_queue_execution_controller",
        lambda **kwargs: controller_kwargs.update(kwargs) or FakeController(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "deliver_bound_chat_assistant_output",
        _fake_deliver,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "notify_managed_thread_terminal_transition",
        _fake_notify,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert controller_kwargs["retain_completed_surface_targets"] is True
    assert callable(controller_kwargs["surface_target_resolver"])
    assert call_order == [
        "progress:start",
        "progress:finalized:ok",
        "progress:finished",
        "progress:targets:managed-turn-1",
        "deliver:assistant",
        "notify:terminal",
        "progress:clear:managed-turn-1",
    ]


@pytest.mark.anyio
async def test_managed_thread_delivery_result_retires_uncovered_progress_after_final_delivery_enqueue(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    call_order: list[str] = []

    async def _fake_deliver(*args, **kwargs):
        _ = args, kwargs
        call_order.append("deliver:assistant")
        return SimpleNamespace(
            target_count=1,
            published_count=1,
            covered_targets=(),
        )

    async def _fake_notify(*args, **kwargs):
        _ = args, kwargs
        call_order.append("notify:terminal")

    async def _fake_cleanup(**kwargs: Any) -> None:
        _ = kwargs
        call_order.append("cleanup:progress")

    monkeypatch.setattr(
        managed_thread_runtime,
        "deliver_bound_chat_assistant_output",
        _fake_deliver,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "notify_managed_thread_terminal_transition",
        _fake_notify,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "cleanup_bound_chat_live_progress_success",
        _fake_cleanup,
    )

    result = await managed_thread_runtime._deliver_managed_thread_execution_result(
        managed_thread_runtime._managed_thread_request_for_app(app),
        thread_store=store,
        thread=created,
        finalized=managed_thread_runtime.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            managed_thread_id=managed_thread_id,
            managed_turn_id="managed-turn-1",
            backend_thread_id="backend-thread-1",
        ),
        response_payload={},
        progress_targets=(("discord", "channel-1"),),
        clear_progress_targets=lambda turn_id: call_order.append(f"clear:{turn_id}"),
    )

    assert result["status"] == "ok"
    assert call_order == [
        "deliver:assistant",
        "notify:terminal",
        "cleanup:progress",
        "clear:managed-turn-1",
    ]


@pytest.mark.anyio
async def test_managed_thread_delivery_result_skips_cleanup_for_covered_progress_targets(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])

    async def _fake_deliver(*args, **kwargs):
        _ = args, kwargs
        return SimpleNamespace(covered_targets=(("discord", "channel-1"),))

    async def _fake_notify(*args, **kwargs):
        _ = args, kwargs

    async def _fake_cleanup(**kwargs: Any) -> None:
        raise AssertionError(f"unexpected cleanup call: {kwargs}")

    monkeypatch.setattr(
        managed_thread_runtime,
        "deliver_bound_chat_assistant_output",
        _fake_deliver,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "notify_managed_thread_terminal_transition",
        _fake_notify,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "cleanup_bound_chat_live_progress_success",
        _fake_cleanup,
    )

    result = await managed_thread_runtime._deliver_managed_thread_execution_result(
        managed_thread_runtime._managed_thread_request_for_app(app),
        thread_store=store,
        thread=created,
        finalized=managed_thread_runtime.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            managed_thread_id=managed_thread_id,
            managed_turn_id="managed-turn-1",
            backend_thread_id="backend-thread-1",
        ),
        response_payload={},
        progress_targets=(("discord", "channel-1"),),
        clear_progress_targets=lambda _turn_id: None,
    )

    assert result["status"] == "ok"


@pytest.mark.anyio
async def test_managed_thread_delivery_result_retires_progress_targets_when_delivery_fails(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    cleared_turn_ids: list[str] = []
    cleanup_calls: list[dict[str, Any]] = []

    async def _fake_deliver(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("delivery failed")

    async def _fake_notify(*args, **kwargs):
        raise AssertionError("notify should not run after delivery failure")

    async def _fake_cleanup_failure(**kwargs: Any) -> None:
        cleanup_calls.append(dict(kwargs))

    monkeypatch.setattr(
        managed_thread_runtime,
        "deliver_bound_chat_assistant_output",
        _fake_deliver,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "notify_managed_thread_terminal_transition",
        _fake_notify,
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "cleanup_bound_chat_live_progress_failure",
        _fake_cleanup_failure,
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await managed_thread_runtime._deliver_managed_thread_execution_result(
            managed_thread_runtime._managed_thread_request_for_app(app),
            thread_store=store,
            thread=created,
            finalized=managed_thread_runtime.ManagedThreadFinalizationResult(
                status="ok",
                assistant_text="assistant-output",
                error=None,
                managed_thread_id=managed_thread_id,
                managed_turn_id="managed-turn-1",
                backend_thread_id="backend-thread-1",
            ),
            response_payload={},
            progress_targets=(("discord", "channel-1"),),
            clear_progress_targets=cleared_turn_ids.append,
        )

    assert cleared_turn_ids == ["managed-turn-1"]
    assert cleanup_calls == [
        {
            "hub_root": app.state.config.root,
            "raw_config": app.state.config.raw,
            "surface_kind": "discord",
            "surface_key": "channel-1",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": "managed-turn-1",
            "failure_message": "Final response delivery failed. Please retry.",
        }
    ]


def test_pma_queue_worker_uses_contextual_delivery_helper_for_progress_targets(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeController:
        def __init__(self) -> None:
            self.hooks = SimpleNamespace()

        def surface_targets_for(
            self, managed_turn_id: str
        ) -> tuple[tuple[str, str], ...]:
            return (("discord", f"target-for:{managed_turn_id}"),)

        def clear_surface_targets(self, managed_turn_id: str) -> None:
            captured["cleared_turn_id"] = managed_turn_id

    def _fake_ensure_queue_worker(*args: Any, **kwargs: Any) -> None:
        captured["deliver_result"] = kwargs["deliver_managed_thread_execution_result"]
        captured["hooks"] = kwargs["hooks"]

    monkeypatch.setattr(
        managed_thread_runtime,
        "build_bound_chat_queue_execution_controller",
        lambda **kwargs: (
            captured.__setitem__("controller_kwargs", dict(kwargs)) or FakeController()
        ),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "ensure_queue_worker",
        _fake_ensure_queue_worker,
    )

    managed_thread_runtime.ensure_managed_thread_queue_worker(app, managed_thread_id)

    hooks = captured["hooks"]
    assert hooks.deliver_result is None
    assert hooks.queue_execution_hooks is not None
    assert captured["controller_kwargs"]["retain_completed_surface_targets"] is True
    assert callable(captured["controller_kwargs"]["surface_target_resolver"])


def test_resolve_pma_chat_bound_surface_targets_uses_execution_metadata() -> None:
    started = SimpleNamespace(
        execution=SimpleNamespace(
            metadata={
                "bound_chat_execution": {
                    "origin": {"kind": "pma_web"},
                    "progress_targets": [
                        {
                            "surface_kind": "discord",
                            "surface_key": "channel-1",
                        },
                        {
                            "surface_kind": "telegram",
                            "surface_key": "chat-1:55",
                        },
                    ],
                }
            }
        )
    )
    service = SimpleNamespace(
        thread_store=SimpleNamespace(get_running_turn=lambda managed_thread_id: None),
        list_bindings=lambda **kwargs: [],
    )

    assert managed_thread_runtime._resolve_pma_chat_bound_surface_targets(
        service=service,
        managed_thread_id="thread-1",
        started=started,
    ) == (("discord", "channel-1"), ("telegram", "chat-1:55"))


def test_resolve_pma_chat_bound_surface_targets_skips_running_turn_for_pre_submit_requests() -> (
    None
):
    service = SimpleNamespace(
        thread_store=SimpleNamespace(
            get_running_turn=lambda managed_thread_id: {
                "metadata": {
                    "bound_chat_execution": {
                        "origin": {
                            "kind": "surface",
                            "surface_kind": "discord",
                            "surface_key": "active-channel",
                        },
                        "progress_targets": [
                            {
                                "surface_kind": "discord",
                                "surface_key": "active-channel",
                            }
                        ],
                    }
                }
            }
        ),
        list_bindings=lambda **kwargs: [
            SimpleNamespace(surface_kind="telegram", surface_key="chat-1:55")
        ],
    )

    assert managed_thread_runtime._resolve_pma_chat_bound_surface_targets(
        service=service,
        managed_thread_id="thread-1",
        started=None,
        allow_running_turn_fallback=False,
    ) == (("telegram", "chat-1:55"),)


def test_managed_thread_message_route_persists_pma_bound_chat_execution_metadata(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    bindings = OrchestrationBindingStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    bindings.upsert_binding(
        surface_kind="discord",
        surface_key="channel-1",
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="100:200",
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def list_bindings(self, **kwargs: Any):
            return bindings.list_bindings(**kwargs)

        thread_store = store

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
                metadata=request.metadata,
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert captured["request"].metadata["bound_chat_execution"] == {
        "origin": {"kind": "pma_web"},
        "progress_targets": [
            {"surface_kind": "discord", "surface_key": "channel-1"},
            {"surface_kind": "telegram", "surface_key": "100:200"},
        ],
    }


def test_managed_thread_message_route_uses_binding_targets_for_queued_pma_execution(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    bindings = OrchestrationBindingStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    bindings.upsert_binding(
        surface_kind="telegram",
        surface_key="100:200",
        thread_target_id=managed_thread_id,
        agent_id="codex",
        repo_id=hub_env.repo_id,
    )
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def list_bindings(self, **kwargs: Any):
            return bindings.list_bindings(**kwargs)

        thread_store = SimpleNamespace(
            get_running_turn=lambda managed_thread_id: {
                "metadata": {
                    "bound_chat_execution": {
                        "origin": {
                            "kind": "surface",
                            "surface_kind": "discord",
                            "surface_key": "active-channel",
                        },
                        "progress_targets": [
                            {
                                "surface_kind": "discord",
                                "surface_key": "active-channel",
                            }
                        ],
                    }
                }
            }
        )

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
                metadata=request.metadata,
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert captured["request"].metadata["bound_chat_execution"] == {
        "origin": {"kind": "pma_web"},
        "progress_targets": [
            {"surface_kind": "telegram", "surface_key": "100:200"},
        ],
    }


def test_managed_thread_message_route_honors_explicit_core_context_profile(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert captured["request"].context_profile == "car_core"
    assert (
        captured["request"]
        .metadata["runtime_prompt"]
        .startswith(format_pma_discoverability_preamble(hub_root=hub_env.hub_root))
    )
    assert "<injected context>" in captured["request"].metadata["runtime_prompt"]


def test_managed_thread_message_route_self_claims_existing_pr_binding(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root, enable_github_polling=True)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"head_branch": "feature/self-claim"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    PrBindingStore(hub_env.hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id=hub_env.repo_id,
        pr_number=17,
        pr_state="open",
        head_branch="feature/self-claim",
        base_branch="main",
    )

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "claim the PR"},
        )

    binding = PrBindingStore(hub_env.hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )
    assert binding is not None
    watch = ScmPollingWatchStore(hub_env.hub_root).get_watch(
        provider="github",
        binding_id=binding.binding_id,
    )

    assert response.status_code == 200
    assert binding.thread_target_id == managed_thread_id
    assert watch is not None
    assert watch.repo_slug == "acme/widgets"
    assert watch.pr_number == 17
    assert watch.workspace_root == str(hub_env.repo_root.resolve())


def test_managed_thread_message_route_self_claims_discovered_pr_binding(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root, enable_github_polling=True)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"head_branch": "feature/discovered"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    arm_watch_calls: list[dict[str, Any]] = []
    polling_raw_configs: list[Optional[dict[str, Any]]] = []

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    class FakeGitHubService:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        def discover_pr_binding_summary(self, *, cwd=None):
            _ = cwd
            return {
                "repo_slug": "acme/widgets",
                "pr_number": 42,
                "pr_state": "open",
                "head_branch": "feature/discovered",
                "base_branch": "main",
            }

    class FakePollingService:
        def __init__(self, *args, **kwargs) -> None:
            _ = args
            polling_raw_configs.append(kwargs.get("raw_config"))

        def arm_watch(self, *, binding, workspace_root, reaction_config=None):
            arm_watch_calls.append(
                {
                    "binding_id": binding.binding_id,
                    "repo_slug": binding.repo_slug,
                    "pr_number": binding.pr_number,
                    "workspace_root": str(workspace_root),
                    "reaction_config": reaction_config,
                }
            )
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="Opened PR: https://github.com/acme/widgets/pull/42",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )
    monkeypatch.setattr(
        managed_thread_pr_binding_module,
        "GitHubService",
        FakeGitHubService,
    )
    monkeypatch.setattr(
        managed_thread_pr_binding_module,
        "GitHubScmPollingService",
        FakePollingService,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "open the PR"},
        )

    binding = PrBindingStore(hub_env.hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=42,
    )

    assert response.status_code == 200
    assert binding is not None
    assert binding.thread_target_id == managed_thread_id
    assert len(arm_watch_calls) == 1
    assert arm_watch_calls[0]["repo_slug"] == "acme/widgets"
    assert arm_watch_calls[0]["pr_number"] == 42
    assert arm_watch_calls[0]["workspace_root"] == str(hub_env.repo_root.resolve())
    assert polling_raw_configs
    assert polling_raw_configs[0] is not None
    assert polling_raw_configs[0]["github"]["automation"]["polling"]["enabled"] is True
    assert (
        arm_watch_calls[0]["reaction_config"]["github"]["automation"]["polling"][
            "enabled"
        ]
        is True
    )


def test_managed_thread_message_route_honors_explicit_approval_override(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(
            self,
            thread_target_id: str,
            execution_id: str,
            *,
            status: str,
            assistant_text: Optional[str] = None,
            error: Optional[str] = None,
            backend_turn_id: Optional[str] = None,
            transcript_turn_id: Optional[str] = None,
        ):
            _ = (
                thread_target_id,
                execution_id,
                assistant_text,
                error,
                backend_turn_id,
                transcript_turn_id,
            )
            return SimpleNamespace(status=status, error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id
        captured["request"] = request
        captured["sandbox_policy"] = sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        create_response = client.post(
            "/hub/pma/threads",
            json={
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": hub_env.repo_id,
                "approval_mode": "read-only",
            },
        )
        assert create_response.status_code == 200
        managed_thread_id = create_response.json()["thread"]["managed_thread_id"]
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert create_response.json()["thread"]["approval_mode"] == "read-only"
    assert response.status_code == 200
    assert captured["request"].approval_mode == "on-request"
    assert captured["sandbox_policy"] == "readOnly"


def test_managed_thread_message_route_injects_core_context_when_profile_is_core(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def get_running_execution(self, thread_target_id: str):
            _ = thread_target_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert captured["request"].context_profile == "car_core"
    assert "<injected context>" in captured["request"].metadata["runtime_prompt"]


def test_managed_thread_message_route_uses_live_runtime_binding_for_compact_seed(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    store.set_thread_compact_seed(managed_thread_id, "compact summary")
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def get_thread_runtime_binding(self, thread_target_id: str):
            _ = thread_target_id
            return SimpleNamespace(backend_thread_id="backend-thread-1")

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    runtime_prompt = captured["request"].metadata["runtime_prompt"]
    assert "Context summary (from compaction):" not in runtime_prompt
    assert "compact summary" not in runtime_prompt
    assert runtime_prompt.endswith("hello from route\n</user_message>\n")


def test_managed_thread_message_route_preserves_literal_message_whitespace(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        captured["request"] = request
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    literal_message = "  keep literal backticks `glm-5-turbo`\nsecond line\n"
    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": literal_message},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["delivered_message"] == literal_message
    assert captured["request"].message_text == literal_message
    runtime_prompt = captured["request"].metadata["runtime_prompt"]
    assert f"<user_message>\n{literal_message}" in runtime_prompt
    assert runtime_prompt.endswith("\n</user_message>\n")


def test_managed_thread_message_route_delegates_harness_to_shared_finalization(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    captured: dict[str, Any] = {}

    class FakeHarness:
        def supports(self, capability: str) -> bool:
            return capability == "event_streaming"

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None, backend_id="backend-turn-1")

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

        def get_running_execution(self, thread_target_id: str):
            _ = thread_target_id
            return None

        def claim_next_queued_execution_request(self, thread_target_id: str):
            _ = thread_target_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-raw-events",
                backend_id="backend-turn-1",
                status="running",
            ),
            thread=SimpleNamespace(
                thread_target_id=managed_thread_id,
                backend_thread_id="backend-thread-1",
            ),
            workspace_root=hub_env.repo_root.resolve(),
            request=request,
            harness=FakeHarness(),
        )

    async def _fake_run_started_execution(
        self: Any,
        started: Any,
        *,
        hooks: Any = None,
        runtime_event_state: Any = None,
    ):
        _ = self, hooks, runtime_event_state
        captured["started"] = started
        return managed_thread_runtime.ManagedThreadFinalizationResult(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            managed_thread_id=managed_thread_id,
            managed_turn_id="managed-turn-raw-events",
            backend_thread_id="backend-thread-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    monkeypatch.setattr(
        managed_thread_runtime.ManagedThreadTurnCoordinator,
        "run_started_execution",
        _fake_run_started_execution,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "hello from route"},
        )

    assert response.status_code == 200
    assert isinstance(captured["started"].harness, FakeHarness)


def test_zeroclaw_managed_thread_projects_compat_agents_file_for_core_profile(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    workspace_root = (
        hub_env.hub_root / ".codex-autorunner" / "runtimes" / "zeroclaw" / "zc-main"
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "zeroclaw",
        workspace_root.resolve(),
        resource_kind="agent_workspace",
        resource_id="zc-main",
        metadata={"context_profile": "car_core"},
    )
    managed_thread_id = str(created["managed_thread_id"])

    class FakeService:
        def get_thread_target(self, thread_target_id: str):
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                backend_thread_id="backend-thread-1",
            )

        def record_execution_result(self, *args, **kwargs):
            _ = args, kwargs
            return SimpleNamespace(status="ok", error=None)

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return None

    async def _fake_begin(
        service, request, *, client_request_id=None, sandbox_policy=None
    ):
        _ = service, client_request_id, sandbox_policy
        return SimpleNamespace(
            execution=SimpleNamespace(
                execution_id="managed-turn-1",
                backend_id="backend-turn-1",
            ),
            thread=SimpleNamespace(backend_thread_id="backend-thread-1"),
            workspace_root=workspace_root.resolve(),
            request=request,
        )

    async def _fake_await(*args, **kwargs):
        _ = args, kwargs
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text="assistant-output",
            error=None,
            backend_thread_id="backend-thread-1",
            backend_turn_id="backend-turn-1",
        )

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )
    monkeypatch.setattr(
        managed_thread_runtime,
        "begin_runtime_thread_execution",
        _fake_begin,
    )
    _patch_outcome_driven_finalization(
        monkeypatch,
        outcome_builder=_fake_await,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/hub/pma/threads/{managed_thread_id}/messages",
            json={"message": "sync runtime context"},
        )

    assert response.status_code == 200
    agents_path = workspace_root / "workspace" / "AGENTS.md"
    assert agents_path.exists()
    assert "# AGENTS" in agents_path.read_text(encoding="utf-8")


def test_managed_thread_interrupt_route_uses_orchestration_service_seam(
    hub_env,
    monkeypatch,
) -> None:
    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="interrupt me")
    managed_turn_id = str(turn["managed_turn_id"])
    store.set_thread_backend_id(managed_thread_id, "backend-thread-1")
    store.set_turn_backend_turn_id(managed_turn_id, "backend-turn-1")
    calls: list[str] = []

    class FakeService:
        def get_thread_runtime_binding(self, thread_target_id: str):
            _ = thread_target_id
            return SimpleNamespace(backend_thread_id="backend-thread-1")

        async def interrupt_thread(self, thread_target_id: str):
            calls.append(thread_target_id)
            store.mark_turn_interrupted(managed_turn_id)
            return SimpleNamespace(status="interrupted")

        async def stop_thread(self, thread_target_id: str):
            calls.append(thread_target_id)
            store.mark_turn_interrupted(managed_turn_id)
            return SimpleNamespace(
                thread_target_id=thread_target_id,
                execution=SimpleNamespace(status="interrupted"),
                interrupted_active=True,
                recovered_lost_backend=False,
                cancelled_queued=0,
            )

        def record_execution_interrupted(
            self, thread_target_id: str, execution_id: str
        ):
            _ = thread_target_id, execution_id
            store.mark_turn_interrupted(execution_id)
            return SimpleNamespace(status="interrupted")

        def get_execution(self, thread_target_id: str, execution_id: str):
            _ = thread_target_id, execution_id
            return SimpleNamespace(status="interrupted")

    monkeypatch.setattr(
        managed_thread_runtime,
        "_build_managed_thread_orchestration_service",
        lambda request, *, thread_store=None: FakeService(),
    )

    with TestClient(app) as client:
        response = client.post(f"/hub/pma/threads/{managed_thread_id}/interrupt")

    assert response.status_code == 200
    assert calls == [managed_thread_id]
    with open_orchestration_sqlite(hub_env.hub_root) as conn:
        row = conn.execute(
            """
            SELECT payload_json
              FROM orch_thread_actions
             ORDER BY CAST(action_id AS INTEGER) DESC
             LIMIT 1
            """
        ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["backend_interrupt_attempted"] is True


def test_interrupt_fails_for_agent_without_interrupt_capability(
    hub_env,
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    app = build_pma_hub_app(hub_env.hub_root)
    store = PmaThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex", hub_env.repo_root.resolve(), repo_id=hub_env.repo_id
    )
    managed_thread_id = str(created["managed_thread_id"])

    def mock_get_running_turn(thread_id: str):
        return {"managed_turn_id": "test-turn-id", "status": "running"}

    monkeypatch.setattr(store, "get_running_turn", mock_get_running_turn)

    def mock_get_available_agents(state):
        return {
            "codex": MagicMock(
                capabilities=frozenset(["durable_threads", "message_turns"])
            )
        }

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_available_agents",
        mock_get_available_agents,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.orchestration.catalog.map_agent_capabilities",
        lambda caps: list(caps),
    )

    with TestClient(app) as client:
        response = client.post(f"/hub/pma/threads/{managed_thread_id}/interrupt")

    assert response.status_code == 403
    assert "does not support interrupt" in response.json()["detail"]
    assert "interrupt" in response.json()["detail"]
