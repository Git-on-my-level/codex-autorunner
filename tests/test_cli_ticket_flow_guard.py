from types import SimpleNamespace

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.cli import app
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.surfaces.cli import cli as cli_module
from codex_autorunner.surfaces.cli.cli import FLOW_COMMANDS


def test_ticket_flow_start_rejects_unregistered_worktree(tmp_path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    seed_hub_files(hub_root, force=True)

    hub_config = load_hub_config(hub_root)
    repo_root = hub_config.worktrees_root / "orphan--branch"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ticket-flow",
            "start",
            "--repo",
            str(repo_root),
            "--hub",
            str(hub_root),
        ],
    )
    assert result.exit_code == 1
    first_line = result.output.splitlines()[0]
    assert (
        first_line
        == "Repo not registered in hub manifest. Run car hub scan or create via car hub worktree create."
    )
    assert str(hub_root) in result.output
    assert str(repo_root) in result.output


def test_resumable_run_prefers_non_latest_active_run() -> None:
    records = [
        FlowRunRecord(
            id="latest-terminal",
            flow_type="ticket_flow",
            status=FlowRunStatus.STOPPED,
            input_data={},
            state={},
            current_step=None,
            stop_requested=False,
            created_at="2026-02-14T10:00:00Z",
            started_at="2026-02-14T10:00:10Z",
            finished_at="2026-02-14T10:00:20Z",
            error_message=None,
            metadata={},
        ),
        FlowRunRecord(
            id="older-active",
            flow_type="ticket_flow",
            status=FlowRunStatus.RUNNING,
            input_data={},
            state={},
            current_step="ticket_turn",
            stop_requested=False,
            created_at="2026-02-14T09:00:00Z",
            started_at="2026-02-14T09:00:05Z",
            finished_at=None,
            error_message=None,
            metadata={},
        ),
    ]

    run, reason = FLOW_COMMANDS.ticket_flow_resumable_run(records)
    assert run is not None
    assert run.id == "older-active"
    assert reason == "active"


def test_flow_command_exports_bridge_uses_typed_attributes(monkeypatch) -> None:
    calls: list[object] = []
    fake_exports = SimpleNamespace(
        _ticket_flow_preflight=lambda engine, ticket_dir: (
            "preflight",
            engine,
            ticket_dir,
        ),
        ticket_flow_print_preflight_report=lambda report: calls.append(
            ("print", report)
        ),
        ticket_flow_start=lambda *args, **kwargs: ("start", args, kwargs),
    )

    monkeypatch.setattr(cli_module, "FLOW_COMMANDS", fake_exports)

    assert cli_module._ticket_flow_preflight("engine", "tickets") == (
        "preflight",
        "engine",
        "tickets",
    )
    cli_module._print_preflight_report("report")
    assert calls == [("print", "report")]
    assert cli_module.ticket_flow_start("repo", force=True) == (
        "start",
        ("repo",),
        {"force": True},
    )
