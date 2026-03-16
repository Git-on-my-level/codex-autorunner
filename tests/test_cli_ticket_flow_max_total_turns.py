from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from codex_autorunner.core.orchestration.models import FlowRunTarget
from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _write_valid_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        """---
ticket_id: "tkt_maxturns001"
agent: codex
done: false
title: "Test ticket"
goal: "Run tests"
---

body
""",
        encoding="utf-8",
    )


def _build_ticket_flow_app(
    monkeypatch, repo_root: Path, start_calls: list[dict]
) -> typer.Typer:
    class _FakeConfig:
        durable_writes = False
        app_server = SimpleNamespace(command=["python"])

    engine = SimpleNamespace(repo_root=repo_root, config=_FakeConfig())

    class _FakeFlowService:
        def list_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            return []

        def list_active_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            return []

        async def start_flow_run(self, flow_target_id, *, input_data=None, metadata=None, run_id=None):  # type: ignore[no-untyped-def]
            _ = flow_target_id
            start_calls.append(
                {
                    "input_data": dict(input_data or {}),
                    "run_id": run_id,
                    "metadata": dict(metadata or {}),
                }
            )
            return FlowRunTarget(
                run_id=run_id or "run-1",
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                status="pending",
                workspace_root=str(repo_root),
                created_at="2026-01-01T00:00:00Z",
                metadata=dict(metadata or {}),
            )

        def get_flow_run(self, run_id: str):  # noqa: ANN001
            return None

        async def stop_flow_run(self, run_id: str):  # noqa: ANN001
            raise AssertionError(f"Unexpected stop_flow_run({run_id})")

    monkeypatch.setattr(
        flow_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: _FakeFlowService(),
    )

    flow_app = typer.Typer(add_completion=False)
    ticket_flow_app = typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: None,
        build_ticket_flow_definition=lambda **_kw: None,
        guard_unregistered_hub_repo=lambda *_args, **_kwargs: None,
        parse_bool_text=lambda *_args, **_kwargs: True,
        parse_duration=lambda *_args, **_kwargs: None,
        cleanup_stale_flow_runs=lambda **_kwargs: 0,
        archive_flow_run_artifacts=lambda **_kwargs: {},
    )
    return ticket_flow_app


def test_ticket_flow_start_forwards_max_total_turns(
    monkeypatch, tmp_path: Path
) -> None:
    _write_valid_ticket(tmp_path)
    start_calls: list[dict] = []
    ticket_flow_app = _build_ticket_flow_app(monkeypatch, tmp_path, start_calls)

    result = CliRunner().invoke(ticket_flow_app, ["start", "--max-total-turns", "200"])

    assert result.exit_code == 0, result.output
    assert len(start_calls) == 1
    assert start_calls[0]["input_data"]["workspace_root"] == str(tmp_path)
    assert start_calls[0]["input_data"]["max_total_turns"] == 200


def test_ticket_flow_bootstrap_forwards_max_total_turns(
    monkeypatch, tmp_path: Path
) -> None:
    _write_valid_ticket(tmp_path)
    start_calls: list[dict] = []
    ticket_flow_app = _build_ticket_flow_app(monkeypatch, tmp_path, start_calls)

    result = CliRunner().invoke(
        ticket_flow_app, ["bootstrap", "--max-total-turns", "120"]
    )

    assert result.exit_code == 0, result.output
    assert len(start_calls) == 1
    assert start_calls[0]["input_data"]["max_total_turns"] == 120
    assert start_calls[0]["metadata"] == {"seeded_ticket": False}
