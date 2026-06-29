from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from codex_autorunner.surfaces.web.services import hub_startup


class _FakeMountManager:
    def __init__(self) -> None:
        self.refreshed = 0
        self.stopped = 0

    async def refresh_mounts(self, snapshots: object) -> None:
        self.refreshed += 1

    async def stop_repo_mounts(self) -> None:
        self.stopped += 1


class _FakeRuntimeServices:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _FakeStaticContext:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _make_context(tmp_path, *, pma_enabled: bool = False, housekeeping: bool = False):
    config = SimpleNamespace(
        root=tmp_path,
        durable_writes=False,
        housekeeping=SimpleNamespace(enabled=housekeeping, interval_seconds=1),
        manifest_path=tmp_path / "manifest.yml",
        pma=SimpleNamespace(enabled=pma_enabled),
        repo_defaults={},
    )
    return SimpleNamespace(
        config=config,
        supervisor=SimpleNamespace(list_repos=lambda use_cache=False: []),
    )


def _make_app(context) -> FastAPI:
    app = FastAPI()
    app.state.config = context.config
    app.state.logger = logging.getLogger("test.hub_startup")
    app.state.hub_deferred_startup_complete = False
    return app


def _patch_deferred_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        hub_startup, "recover_orphaned_managed_thread_executions", _noop_async
    )
    monkeypatch.setattr(
        hub_startup, "restart_managed_thread_queue_workers", _noop_async
    )
    monkeypatch.setattr(
        hub_startup,
        "reap_managed_processes",
        lambda _root: SimpleNamespace(killed=0, signaled=0, removed=0, skipped=0),
    )


@pytest.mark.asyncio
async def test_deferred_startup_marks_complete_on_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _make_context(tmp_path)
    app = _make_app(context)
    _patch_deferred_dependencies(monkeypatch)
    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=_FakeMountManager(),
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    await service.run_deferred_startup(app)

    assert app.state.hub_deferred_startup_complete is True


@pytest.mark.asyncio
async def test_deferred_startup_continues_after_managed_thread_restore_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    context = _make_context(tmp_path)
    app = _make_app(context)

    async def _fail_restore(_app) -> None:
        raise RuntimeError("restore failed")

    monkeypatch.setattr(
        hub_startup, "recover_orphaned_managed_thread_executions", _fail_restore
    )
    monkeypatch.setattr(
        hub_startup, "restart_managed_thread_queue_workers", lambda _app: None
    )
    monkeypatch.setattr(
        hub_startup,
        "reap_managed_processes",
        lambda _root: SimpleNamespace(killed=0, signaled=0, removed=0, skipped=0),
    )
    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=_FakeMountManager(),
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    with caplog.at_level(logging.WARNING):
        await service.run_deferred_startup(app)

    assert app.state.hub_deferred_startup_complete is True
    assert "Managed-thread queue worker restore failed at hub startup" in caplog.text


@pytest.mark.asyncio
async def test_managed_thread_queue_starter_marshals_to_event_loop_from_background_thread(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _make_context(tmp_path)
    app = _make_app(context)

    captured_starters: list[Callable[[str], None]] = []
    loop_thread_id = threading.get_ident()
    observed: dict[str, int | str] = {}
    started = asyncio.Event()

    app.state.hub_supervisor = SimpleNamespace(
        set_managed_thread_queue_worker_starter=lambda starter: captured_starters.append(
            starter
        ),
    )

    def _fake_ensure(_app: object, thread_id: str) -> None:
        observed["thread_id"] = thread_id
        observed["caller_thread"] = threading.get_ident()
        started.set()

    monkeypatch.setattr(hub_startup, "ensure_managed_thread_queue_worker", _fake_ensure)

    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=_FakeMountManager(),
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    service._register_managed_thread_queue_starter(app)
    assert len(captured_starters) == 1
    starter = captured_starters[0]

    background_thread_id: list[int] = []

    def _invoke_from_background() -> None:
        background_thread_id.append(threading.get_ident())
        starter("thread-scm")

    bg = threading.Thread(target=_invoke_from_background)
    bg.start()
    bg.join(timeout=5.0)

    await asyncio.wait_for(started.wait(), timeout=5.0)
    assert observed["thread_id"] == "thread-scm"
    assert observed["caller_thread"] == loop_thread_id
    assert observed["caller_thread"] != background_thread_id[0]


@pytest.mark.asyncio
async def test_deferred_startup_continues_after_pma_lane_restore_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    context = _make_context(tmp_path, pma_enabled=True)
    app = _make_app(context)
    app.state.pma_lane_worker_start = object()
    _patch_deferred_dependencies(monkeypatch)

    async def _fail_lanes(_app, _starter) -> list[str]:
        raise sqlite3.Error("lane db failed")

    monkeypatch.setattr(hub_startup, "start_replayable_pma_lane_workers", _fail_lanes)
    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=_FakeMountManager(),
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    with caplog.at_level(logging.WARNING):
        await service.run_deferred_startup(app)

    assert app.state.hub_deferred_startup_complete is True
    assert "PMA lane worker startup failed" in caplog.text


@pytest.mark.asyncio
async def test_housekeeping_loop_logs_expected_failures_and_keeps_running(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    context = _make_context(tmp_path, housekeeping=True)
    app = _make_app(context)
    sleeps = 0

    async def _fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError()

    async def _fake_to_thread(fn, *args, **kwargs):
        if fn is hub_startup.prune_filebox_root:
            return SimpleNamespace(
                inbox_pruned=0,
                outbox_pruned=0,
                bytes_before=0,
                bytes_after=0,
            )
        if fn is hub_startup.run_housekeeping_once:
            raise RuntimeError("housekeeping failed")
        return SimpleNamespace(to_dict=lambda: {})

    monkeypatch.setattr(hub_startup.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(hub_startup.asyncio, "to_thread", _fake_to_thread)
    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=_FakeMountManager(),
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await service._housekeeping_loop(app, initial_delay=0, interval=1)

    assert "Housekeeping task failed" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_cancels_tasks_and_closes_ports(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _make_context(tmp_path)
    app = _make_app(context)
    mount_manager = _FakeMountManager()
    runtime_services = _FakeRuntimeServices()
    static_context = _FakeStaticContext()
    app.state.runtime_services = runtime_services
    app.state.web_static_assets_context = static_context
    app.state.hub_supervisor = SimpleNamespace(
        startup=lambda: None, shutdown=lambda: None
    )

    class _Hooks:
        restored = 0

        def restore(self) -> None:
            self.restored += 1

    hooks = _Hooks()
    monkeypatch.setattr(hub_startup, "record_hub_startup", lambda *a, **k: None)
    monkeypatch.setattr(hub_startup, "record_hub_clean_shutdown", lambda *a, **k: None)
    monkeypatch.setattr(
        hub_startup, "install_hub_exception_hooks", lambda *a, **k: hooks
    )
    monkeypatch.setattr(
        hub_startup, "record_process_monitor_sample", lambda _root: None
    )
    service = hub_startup.HubStartupService(
        context=context,
        mount_manager=mount_manager,
        endpoint_host=None,
        endpoint_port=None,
        base_path=None,
    )

    async def _sleeping_deferred(_app) -> None:
        await asyncio.sleep(3600)

    service.run_deferred_startup = _sleeping_deferred  # type: ignore[method-assign]

    async with service.lifespan(app):
        assert app.state.hub_started is True

    assert mount_manager.stopped == 1
    assert runtime_services.closed == 1
    assert static_context.closed == 1
    assert hooks.restored == 1
