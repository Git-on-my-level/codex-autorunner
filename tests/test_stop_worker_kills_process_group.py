from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer
from typer.testing import CliRunner

from codex_autorunner.core.flows import worker_process
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.orchestration.models import FlowRunTarget
from codex_autorunner.flows.ticket_flow import runtime_helpers
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


def _build_ticket_flow_app(
    monkeypatch, tmp_path: Path, run_id: str, events: list[str], health
) -> typer.Typer:
    class _FakeConfig:
        durable_writes = False

    engine = SimpleNamespace(repo_root=tmp_path, config=_FakeConfig())

    class _FakeFlowStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def initialize(self) -> None:
            return

        def get_flow_run(self, requested_run_id: str) -> Optional[FlowRunRecord]:
            if requested_run_id == run_id:
                return _record(run_id, FlowRunStatus.RUNNING)
            return None

        def close(self) -> None:
            return

    class _FakeFlowService:
        def list_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            return [
                FlowRunTarget(
                    run_id=run_id,
                    flow_target_id="ticket_flow",
                    flow_type="ticket_flow",
                    status="running",
                    current_step="ticket_turn",
                    workspace_root=str(tmp_path),
                    created_at="2026-02-15T00:00:00Z",
                    started_at="2026-02-15T00:00:01Z",
                )
            ]

        def get_flow_run(self, requested_run_id: str):  # noqa: ANN001
            if requested_run_id != run_id:
                return None
            return FlowRunTarget(
                run_id=run_id,
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                status="running",
                current_step="ticket_turn",
                workspace_root=str(tmp_path),
                created_at="2026-02-15T00:00:00Z",
                started_at="2026-02-15T00:00:01Z",
            )

        def list_active_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            return self.list_flow_runs(flow_target_id=flow_target_id)

        async def stop_flow_run(self, _requested_run_id: str) -> FlowRunTarget:
            events.append("stop_flow")
            return FlowRunTarget(
                run_id=run_id,
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                status="stopped",
                current_step="ticket_turn",
                workspace_root=str(tmp_path),
                created_at="2026-02-15T00:00:00Z",
                started_at="2026-02-15T00:00:01Z",
                finished_at="2026-02-15T00:00:02Z",
            )

        async def start_flow_run(self, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("Unexpected start_flow_run")

    monkeypatch.setattr(flow_module, "FlowStore", _FakeFlowStore)
    monkeypatch.setattr(
        flow_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: _FakeFlowService(),
    )
    monkeypatch.setattr(
        runtime_helpers, "check_worker_health", lambda *_a, **_k: health
    )
    monkeypatch.setattr(
        runtime_helpers, "clear_worker_metadata", lambda *_a, **_k: None
    )

    flow_app = typer.Typer(add_completion=False)
    ticket_flow_app = typer.Typer(add_completion=False)
    telemetry_app = typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        telemetry_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: None,
        build_ticket_flow_definition=lambda **_kw: None,
        guard_unregistered_hub_repo=lambda *_a, **_k: None,
        parse_bool_text=lambda *_a, **_k: True,
        parse_duration=lambda *_a, **_k: None,
        cleanup_stale_flow_runs=lambda **_kw: 0,
        archive_flow_run_artifacts=lambda **_kw: {},
    )
    return ticket_flow_app


def test_ticket_flow_stop_prefers_killpg(monkeypatch, tmp_path: Path) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    events: list[str] = []
    health = SimpleNamespace(
        status="alive",
        pid=42424,
        artifact_path=tmp_path / ".codex-autorunner" / "flows" / run_id / "worker.json",
    )
    ticket_flow_app = _build_ticket_flow_app(
        monkeypatch, tmp_path, run_id, events, health
    )

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(flow_module.os, "name", "posix", raising=False)
    monkeypatch.setattr(
        flow_module.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
    )
    monkeypatch.setattr(
        flow_module.os, "kill", lambda pid, sig: kill_calls.append((pid, sig))
    )

    result = CliRunner().invoke(ticket_flow_app, ["stop", "--run-id", run_id])

    assert result.exit_code == 0, result.output
    assert killpg_calls == [(42424, signal.SIGTERM)]
    assert kill_calls == []
    assert "stop_flow" in events


def test_ticket_flow_stop_falls_back_to_kill(monkeypatch, tmp_path: Path) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    events: list[str] = []
    health = SimpleNamespace(
        status="alive",
        pid=51515,
        artifact_path=tmp_path / ".codex-autorunner" / "flows" / run_id / "worker.json",
    )
    ticket_flow_app = _build_ticket_flow_app(
        monkeypatch, tmp_path, run_id, events, health
    )

    killpg_calls: list[tuple[int, int]] = []
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(flow_module.os, "name", "posix", raising=False)

    def _killpg(_pgid: int, _sig: int) -> None:
        killpg_calls.append((_pgid, _sig))
        raise OSError("killpg failed")

    monkeypatch.setattr(flow_module.os, "killpg", _killpg)
    monkeypatch.setattr(
        flow_module.os, "kill", lambda pid, sig: kill_calls.append((pid, sig))
    )

    result = CliRunner().invoke(ticket_flow_app, ["stop", "--run-id", run_id])

    assert result.exit_code == 0, result.output
    assert killpg_calls == [(51515, signal.SIGTERM)]
    assert kill_calls == [(51515, signal.SIGTERM)]


def test_spawn_flow_worker_uses_new_session(monkeypatch, tmp_path: Path) -> None:
    run_id = "3022db08-82b8-40dd-8cfa-d04eb0fcded2"
    seen: dict[str, object] = {}

    class _FakeProc:
        pid = 777

    def _fake_popen(*_args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(worker_process.subprocess, "Popen", _fake_popen)

    proc, out, err = worker_process.spawn_flow_worker(tmp_path, run_id)
    out.close()
    err.close()

    assert proc.pid == 777
    assert seen.get("start_new_session") is True
