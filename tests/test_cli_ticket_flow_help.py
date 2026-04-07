from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.orchestration.models import FlowRunTarget
from codex_autorunner.surfaces.cli.commands import flow as flow_module


def _write_valid_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        """---
ticket_id: "tkt_help001"
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


def _build_ticket_flow_app(
    monkeypatch, repo_root: Path, *, service_factory=None
) -> typer.Typer:
    class _FakeConfig:
        durable_writes = False
        app_server = SimpleNamespace(command=["python"])

    engine = SimpleNamespace(repo_root=repo_root, config=_FakeConfig())

    class _FakeFlowController:
        def list_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            with FlowStore(repo_root / ".codex-autorunner" / "flows.db") as store:
                store.initialize()
                return [
                    FlowRunTarget(
                        run_id=record.id,
                        flow_target_id="ticket_flow",
                        flow_type=record.flow_type,
                        status=record.status.value,
                        current_step=record.current_step,
                        workspace_root=str(repo_root),
                        created_at=record.created_at,
                        started_at=record.started_at,
                        finished_at=record.finished_at,
                        error_message=record.error_message,
                        state=dict(record.state or {}),
                        metadata=dict(record.metadata or {}),
                    )
                    for record in store.list_flow_runs(flow_type=flow_target_id)
                ]

        def list_active_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            return []

        async def start_flow_run(
            self, flow_target_id, *, input_data=None, metadata=None, run_id=None
        ):  # type: ignore[no-untyped-def]
            _ = (flow_target_id, input_data, metadata)
            return FlowRunTarget(
                run_id=run_id or "run-1",
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                status="pending",
                workspace_root=str(repo_root),
                created_at="2026-01-01T00:00:00Z",
            )

        def get_flow_run(self, run_id: str):  # noqa: ANN001
            return None

        async def stop_flow_run(self, run_id: str):  # noqa: ANN001
            raise AssertionError(f"Unexpected stop_flow_run({run_id})")

    monkeypatch.setattr(
        flow_module,
        "build_ticket_flow_orchestration_service",
        service_factory or (lambda *, workspace_root: _FakeFlowController()),
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
    # Removed subcommand is `resume`; avoid embedding `car ... resume` (hint linter).
    assert "ticket_flow resume" not in result.output


def test_ticket_flow_status_reads_run_via_orchestration_service(
    monkeypatch, tmp_path: Path
) -> None:
    _write_valid_ticket(tmp_path)
    run_id = "22222222-2222-2222-2222-222222222222"
    observed: dict[str, object] = {}

    class _Service:
        def list_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            observed["list_flow_runs"] = flow_target_id
            return []

        def list_active_flow_runs(self, *, flow_target_id=None):  # noqa: ANN001
            observed["list_active_flow_runs"] = flow_target_id
            return []

        def get_flow_run(self, requested_run_id: str):
            observed["get_flow_run"] = requested_run_id
            return FlowRunTarget(
                run_id=run_id,
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                status="paused",
                workspace_root=str(tmp_path),
                created_at="2026-01-01T00:00:00Z",
            )

        async def start_flow_run(self, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("Unexpected start_flow_run")

        async def stop_flow_run(self, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("Unexpected stop_flow_run")

    ticket_flow_app = _build_ticket_flow_app(
        monkeypatch,
        tmp_path,
        service_factory=lambda *, workspace_root: _Service(),
    )

    result = CliRunner().invoke(
        ticket_flow_app,
        ["status", "--run-id", run_id, "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed == {"get_flow_run": run_id}
