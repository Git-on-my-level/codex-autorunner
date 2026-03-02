from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _write_valid_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        """---
agent: codex
done: false
title: "Test ticket"
goal: "Run tests"
---

body
""",
        encoding="utf-8",
    )


def _seed_stale_stopped_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(run_id, "ticket_flow", {})
        store.update_flow_run_status(run_id, FlowRunStatus.STOPPED)


def _build_ticket_flow_app(monkeypatch, repo_root: Path) -> typer.Typer:
    class _FakeConfig:
        durable_writes = False
        app_server = SimpleNamespace(command=["python"])

    engine = SimpleNamespace(repo_root=repo_root, config=_FakeConfig())

    class _FakeFlowController:
        def __init__(self, **_kwargs) -> None:
            pass

        def initialize(self) -> None:
            return

        async def start_flow(self, input_data, run_id, metadata=None):  # type: ignore[no-untyped-def]
            _ = (input_data, metadata)
            return SimpleNamespace(id=run_id)

        def shutdown(self) -> None:
            return

    class _FakeDefinition:
        def validate(self) -> None:
            return

    class _FakeAgentPool:
        async def close_all(self) -> None:
            return

    monkeypatch.setattr(flow_module, "FlowController", _FakeFlowController)
    monkeypatch.setattr(
        flow_module, "ensure_worker", lambda *_args, **_kwargs: {"status": "reused"}
    )

    flow_app = typer.Typer(add_completion=False)
    ticket_flow_app = typer.Typer(add_completion=False)
    flow_module.register_flow_commands(
        flow_app,
        ticket_flow_app,
        require_repo_config=lambda _repo, _hub: engine,
        raise_exit=lambda msg, **_kw: (_ for _ in ()).throw(RuntimeError(msg)),
        build_agent_pool=lambda _cfg: _FakeAgentPool(),
        build_ticket_flow_definition=lambda **_kw: _FakeDefinition(),
        guard_unregistered_hub_repo=lambda *_args, **_kwargs: None,
        parse_bool_text=lambda *_args, **_kwargs: True,
        parse_duration=lambda *_args, **_kwargs: None,
        cleanup_stale_flow_runs=lambda **_kwargs: 0,
        archive_flow_run_artifacts=lambda **_kwargs: {},
    )
    return ticket_flow_app


def test_ticket_flow_start_help_includes_discoverability_breadcrumbs() -> None:
    result = CliRunner().invoke(app, ["flow", "ticket_flow", "start", "--help"])
    assert result.exit_code == 0
    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    clean = " ".join(clean.split())
    assert "Run preflight checks first:" in clean
    assert "car flow ticket_flow preflight" in clean
    assert "Inspect run details:" in clean
    assert "car flow ticket_flow status --run-id <run_id>" in clean


def test_ticket_flow_bootstrap_help_includes_discoverability_breadcrumbs() -> None:
    result = CliRunner().invoke(app, ["flow", "ticket_flow", "bootstrap", "--help"])
    assert result.exit_code == 0
    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    clean = " ".join(clean.split())
    assert "Inspect all ticket_flow commands:" in clean
    assert "car flow ticket_flow --help" in clean
    assert "Check run health:" in clean
    assert "car flow ticket_flow status --run-id <run_id>" in clean


def test_ticket_flow_start_stale_warning_uses_existing_cli_commands(
    monkeypatch, tmp_path: Path
) -> None:
    _write_valid_ticket(tmp_path)
    stale_run_id = "11111111-1111-1111-1111-111111111111"
    _seed_stale_stopped_run(tmp_path, stale_run_id)
    ticket_flow_app = _build_ticket_flow_app(monkeypatch, tmp_path)

    result = CliRunner().invoke(ticket_flow_app, ["start"])

    assert result.exit_code == 0, result.output
    assert "stale run(s) found" in result.output
    assert f"car flow ticket_flow status --run-id {stale_run_id}" in result.output
    assert (
        f"car flow ticket_flow archive --run-id {stale_run_id} --force" in result.output
    )
    assert "car flow ticket_flow resume --run-id" not in result.output
