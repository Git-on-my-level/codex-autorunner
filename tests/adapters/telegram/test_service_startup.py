from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.adapters.chat import (
    managed_thread_startup_recovery as chat_startup_recovery_module,
)
from codex_autorunner.adapters.telegram import (
    managed_thread_startup_recovery as telegram_startup_recovery_module,
)

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_startup_recovery_registers_running_execution_reattach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_recover_surface_managed_thread_executions_on_startup(
        service: object,
        **kwargs: object,
    ) -> None:
        _ = service
        captured.update(kwargs)

    monkeypatch.setattr(
        telegram_startup_recovery_module,
        "recover_surface_managed_thread_executions_on_startup",
        _fake_recover_surface_managed_thread_executions_on_startup,
    )

    service = SimpleNamespace(
        _config=SimpleNamespace(root=tmp_path),
        _logger=logging.getLogger("test.telegram.startup.reattach_hook"),
    )

    await telegram_startup_recovery_module.recover_managed_thread_executions_on_startup(
        service
    )

    assert captured["surface_kind"] == "telegram"
    reattach_running_execution = captured["reattach_running_execution"]
    assert callable(reattach_running_execution)


@pytest.mark.anyio
async def test_reattach_running_execution_runs_and_rearms_queue_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_finalized: list[str] = []
    base_finalized: list[str] = []
    handoffs: list[object] = []
    ensured_workers: list[str] = []
    spawned_tasks: list[asyncio.Task[object]] = []

    async def _startup_on_execution_finalized(
        _started: object,
        finalized: object,
    ) -> None:
        startup_finalized.append(str(finalized.managed_turn_id))

    async def _base_on_execution_finalized(_started: object, finalized: object) -> None:
        base_finalized.append(str(finalized.managed_turn_id))

    class FakeCoordinator:
        async def run_started_execution(
            self,
            started: object,
            *,
            hooks: object,
            runtime_event_state: object,
            record_finalization_failure: bool,
        ) -> object:
            _ = runtime_event_state
            assert record_finalization_failure is True
            assert started.request.message_text == "reattached prompt"
            finalized = SimpleNamespace(
                status="ok",
                managed_turn_id="turn-1",
                managed_thread_id="thread-1",
                assistant_text="done",
                error=None,
                backend_thread_id="backend-thread-1",
            )
            await hooks.on_execution_finalized(started, finalized)
            return finalized

        def ensure_queue_worker(self, **kwargs: object) -> None:
            ensured_workers.append(str(kwargs["managed_thread_id"]))

    def _fake_runner_hooks(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            queue_worker_hooks=lambda: SimpleNamespace(
                durable_delivery=object(),
                execution_hooks=telegram_startup_recovery_module.ManagedThreadExecutionHooks(
                    on_execution_finalized=_base_on_execution_finalized
                ),
            )
        )

    async def _fake_handoff(finalized: object, **_kwargs: object) -> None:
        handoffs.append(finalized)

    monkeypatch.setattr(
        telegram_startup_recovery_module,
        "_build_telegram_managed_thread_coordinator",
        lambda *_args, **_kwargs: FakeCoordinator(),
    )
    monkeypatch.setattr(
        telegram_startup_recovery_module,
        "_build_telegram_runner_hooks",
        _fake_runner_hooks,
    )
    monkeypatch.setattr(
        telegram_startup_recovery_module,
        "_build_telegram_startup_recovery_execution_hooks",
        lambda *_args, **_kwargs: telegram_startup_recovery_module.ManagedThreadExecutionHooks(
            on_execution_finalized=_startup_on_execution_finalized
        ),
    )
    monkeypatch.setattr(
        chat_startup_recovery_module,
        "handoff_managed_thread_final_delivery",
        _fake_handoff,
    )
    monkeypatch.setattr(
        telegram_startup_recovery_module,
        "_spawn_telegram_background_task",
        lambda _service, coro: spawned_tasks.append(asyncio.create_task(coro))
        or spawned_tasks[-1],
    )

    service = SimpleNamespace(
        _config=SimpleNamespace(root=tmp_path),
        _logger=logging.getLogger("test.telegram.startup.reattach"),
    )
    orchestration_service = SimpleNamespace(
        thread_store=SimpleNamespace(
            get_running_turn=lambda _thread_id: {"prompt": "reattached prompt"}
        ),
        _harness_for_thread=lambda _thread: object(),
    )

    reattached = telegram_startup_recovery_module.reattach_running_telegram_managed_thread_execution(
        service,
        orchestration_service=orchestration_service,
        surface_key="123:456",
        managed_thread_id="thread-1",
        thread=SimpleNamespace(
            workspace_root=tmp_path,
            backend_thread_id="backend-thread-1",
        ),
        execution=SimpleNamespace(
            execution_id="turn-1",
            backend_id="backend-turn-1",
        ),
    )

    assert reattached.kind == "reattached"
    assert bool(reattached) is True
    assert len(spawned_tasks) == 1
    assert service._telegram_managed_thread_queue_tasks["thread-1"] is spawned_tasks[0]
    await spawned_tasks[0]

    assert base_finalized == ["turn-1"]
    assert startup_finalized == ["turn-1"]
    assert len(handoffs) == 1
    assert ensured_workers == ["thread-1"]
    assert service._telegram_managed_thread_queue_tasks == {}


@pytest.mark.anyio
async def test_reattach_running_execution_reports_missing_harness_or_unsupported(
    tmp_path: Path,
) -> None:
    service = SimpleNamespace(
        _config=SimpleNamespace(root=tmp_path),
        _logger=logging.getLogger("test.telegram.startup.missing_harness"),
    )
    orchestration_service = SimpleNamespace(
        thread_store=SimpleNamespace(get_running_turn=lambda _thread_id: {}),
    )

    result = telegram_startup_recovery_module.reattach_running_telegram_managed_thread_execution(
        service,
        orchestration_service=orchestration_service,
        surface_key="123:456",
        managed_thread_id="thread-1",
        thread=SimpleNamespace(
            workspace_root=tmp_path,
            backend_thread_id="backend-thread-1",
        ),
        execution=SimpleNamespace(
            execution_id="turn-1",
            backend_id="backend-turn-1",
        ),
    )

    assert result.kind == "missing_harness_or_unsupported"
    assert bool(result) is False
