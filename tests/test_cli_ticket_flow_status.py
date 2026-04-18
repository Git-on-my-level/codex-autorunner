from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore

runner = CliRunner()


def _setup_paused_run(repo_root: Path, run_id: str) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_status001"\nagent: user\ndone: false\n---\n\nStatus ticket\n',
        encoding="utf-8",
    )

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={
                "reason_summary": "Paused for user input.",
                "ticket_engine": {
                    "reason": (
                        "Paused for user input. Mark ticket as done when ready: "
                        ".codex-autorunner/tickets/TICKET-001.md"
                    ),
                    "reason_code": "user_pause",
                },
            },
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def test_ticket_flow_status_includes_pause_reason_in_human_output(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(tmp_path, force=True)
    seed_repo_files(repo_root, git_required=False)

    run_id = "12121212-1212-1212-1212-121212121212"
    _setup_paused_run(repo_root, run_id)

    result = runner.invoke(
        app, ["ticket-flow", "status", "--repo", str(repo_root), "--run-id", run_id]
    )

    assert result.exit_code == 0
    assert "summary: Paused for user input." in result.stdout
    assert "reason_code: user_pause" in result.stdout
    assert "reason: Paused for user input. Mark ticket as done when ready:" in (
        result.stdout
    )


def test_ticket_flow_status_includes_pause_reason_in_json_output(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(tmp_path, force=True)
    seed_repo_files(repo_root, git_required=False)

    run_id = "34343434-3434-3434-3434-343434343434"
    _setup_paused_run(repo_root, run_id)

    result = runner.invoke(
        app,
        [
            "ticket-flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["reason_summary"] == "Paused for user input."
    assert payload["reason_code"] == "user_pause"
    assert payload["reason"].startswith(
        "Paused for user input. Mark ticket as done when ready:"
    )
