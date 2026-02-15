from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from typer.testing import CliRunner

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _record(run_id: str, status: FlowRunStatus) -> FlowRunRecord:
    return FlowRunRecord(
        id=run_id,
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state={},
        current_step="ticket_turn",
        stop_requested=False,
        created_at="2026-02-15T00:00:00Z",
        started_at="2026-02-15T00:00:01Z",
        finished_at=None,
        error_message=None,
        metadata={},
    )


def test_flow_worker_sigterm_closes_agent_pool_and_controller(
    monkeypatch, tmp_path: Path
) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        """---
title: "Test ticket"
agent: "codex"
done: false
goal: "run"
---

body
""",
        encoding="utf-8",
    )

    events: list[str] = []
    handlers: dict[int, Callable] = {}

    class _FakeConfig:
        durable_writes = False
        app_server = SimpleNamespace(command=["python"])

        def agent_serve_command(self, _agent: str) -> Optional[list[str]]:
            return None

        def agent_binary(self, _agent: str) -> str:
            return "opencode"

    engine = SimpleNamespace(repo_root=tmp_path, config=_FakeConfig())

    class _FakeFlowStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def initialize(self) -> None:
            return

        def get_flow_run(self, requested_run_id: str) -> Optional[FlowRunRecord]:
            if requested_run_id == run_id:
                return _record(run_id, FlowRunStatus.PENDING)
            return None

        def close(self) -> None:
            return

    class _FakeController:
        def __init__(self, **_kwargs) -> None:
            self.store = SimpleNamespace(get_last_event_by_type=lambda *_a, **_k: None)

        def initialize(self) -> None:
            return

        def get_status(self, requested_run_id: str) -> Optional[FlowRunRecord]:
            if requested_run_id == run_id:
                return _record(run_id, FlowRunStatus.PENDING)
            return None

        async def run_flow(self, _requested_run_id: str) -> FlowRunRecord:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("run_flow_cancelled")
                raise

        async def stop_flow(self, _requested_run_id: str) -> FlowRunRecord:
            events.append("stop_flow")
            return _record(run_id, FlowRunStatus.STOPPED)

        def shutdown(self) -> None:
            events.append("controller_shutdown")

    class _FakeDefinition:
        def validate(self) -> None:
            return

    class _FakeAgentPool:
        async def close_all(self) -> None:
            events.append("agent_pool_close")

    monkeypatch.setattr(flow_module, "FlowStore", _FakeFlowStore)
    monkeypatch.setattr(flow_module, "FlowController", _FakeController)
    monkeypatch.setattr(flow_module, "register_worker_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flow_module, "resolve_executable", lambda *_a, **_k: "/bin/true"
    )
    monkeypatch.setattr(
        flow_module, "atexit", SimpleNamespace(register=lambda _fn: None)
    )
    monkeypatch.setattr(
        flow_module,
        "signal",
        SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIGINT=signal.SIGINT,
            signal=lambda sig, fn: handlers.__setitem__(sig, fn),
        ),
    )
    monkeypatch.setattr(
        flow_module,
        "write_worker_exit_info",
        lambda *_a, **_k: events.append("write_exit"),
    )

    flow_app = flow_module.typer.Typer(add_completion=False)
    ticket_flow_app = flow_module.typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: _FakeAgentPool(),
        build_ticket_flow_definition=lambda **_kw: _FakeDefinition(),
        guard_unregistered_hub_repo=lambda *_a, **_k: None,
        parse_bool_text=lambda *_a, **_k: True,
        parse_duration=lambda *_a, **_k: None,
        cleanup_stale_flow_runs=lambda **_kw: 0,
        archive_flow_run_artifacts=lambda **_kw: {},
    )

    result = CliRunner().invoke(flow_app, ["--run-id", run_id])

    assert result.exit_code == 0, result.output
    assert "stop_flow" in events
    assert "run_flow_cancelled" in events
    assert "controller_shutdown" in events
    assert "agent_pool_close" in events
    assert "write_exit" in events
    assert events.index("controller_shutdown") < events.index("write_exit")
    assert events.index("agent_pool_close") < events.index("write_exit")
