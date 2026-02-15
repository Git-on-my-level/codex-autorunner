from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from codex_autorunner.core.runtime_services import RuntimeServices
from codex_autorunner.surfaces.web.app_builders import _app_lifespan


class _FakeAgentPool:
    def __init__(self) -> None:
        self.closed = 0

    async def close_all(self) -> None:
        self.closed += 1


class _FakeController:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeSupervisor:
    def __init__(self) -> None:
        self.closed = 0

    async def close_all(self) -> None:
        self.closed += 1


def test_runtime_services_close_closes_owned_resources(tmp_path: Path) -> None:
    pools: list[_FakeAgentPool] = []
    controllers: list[_FakeController] = []

    def _build(_repo_root: Path):
        pool = _FakeAgentPool()
        controller = _FakeController()
        pools.append(pool)
        controllers.append(controller)
        return SimpleNamespace(controller=controller, agent_pool=pool)

    app_server_supervisor = _FakeSupervisor()
    opencode_supervisor = _FakeSupervisor()
    services = RuntimeServices(
        app_server_supervisor=app_server_supervisor,
        opencode_supervisor=opencode_supervisor,
        flow_runtime_builder=_build,
    )

    first = services.get_ticket_flow_controller(tmp_path)
    second = services.get_ticket_flow_controller(tmp_path)
    assert first is second
    assert len(controllers) == 1

    asyncio.run(services.close())
    assert controllers[0].shutdown_calls == 1
    assert pools[0].closed == 1
    assert app_server_supervisor.closed == 1
    assert opencode_supervisor.closed == 1


@pytest.mark.asyncio
async def test_repo_app_lifespan_closes_runtime_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_services = SimpleNamespace(closed=0)

    async def _close_runtime_services() -> None:
        runtime_services.closed += 1

    runtime_services.close = _close_runtime_services  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.app_builders.reconcile_flow_runs",
        lambda *args, **kwargs: SimpleNamespace(summary=SimpleNamespace(active=0)),
    )
    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.app_builders.persist_session_registry",
        lambda *args, **kwargs: None,
    )

    context = SimpleNamespace(tui_idle_seconds=None, tui_idle_check_seconds=None)
    app = FastAPI()
    app.state.logger = logging.getLogger("test")
    app.state.terminal_lock = asyncio.Lock()
    app.state.terminal_sessions = {}
    app.state.session_registry = {}
    app.state.repo_to_session = {}
    app.state.terminal_max_idle_seconds = None
    app.state.engine = SimpleNamespace(
        state_path=tmp_path / "state.json", repo_root=tmp_path, notifier=None
    )
    app.state.config = SimpleNamespace(
        housekeeping=SimpleNamespace(enabled=False, interval_seconds=1)
    )
    app.state.runtime_services = runtime_services
    app.state.static_assets_context = None

    lifespan = _app_lifespan(context)
    async with lifespan(app):
        pass

    assert runtime_services.closed == 1
