from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.cli import app
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore

runner = CliRunner()


def _seed_repo_run(repo_root: Path, run_id: str, status: FlowRunStatus) -> None:
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
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, status)


def _seed_ticket(repo_root: Path) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: user\ndone: false\n---\n\nStatus ticket\n",
        encoding="utf-8",
    )


def test_ticket_flow_archive_moves_run_artifacts_and_deletes_run(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    run_id = "99999999-9999-9999-9999-999999999999"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.STOPPED)

    run_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "DISPATCH.md").write_text(
        "---\nmode: pause\n---\n\nhello\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["archived_runs"] is True
    assert payload["deleted_run"] is True

    archived_root = repo_root / ".codex-autorunner" / "flows" / run_id / "archived_runs"
    assert archived_root.exists()

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        assert store.get_flow_run(run_id) is None


def test_ticket_flow_archive_dry_run_does_not_modify(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)

    run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.FAILED)

    run_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "archive",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["archived_runs"] is False
    assert payload["deleted_run"] is False
    assert run_dir.exists()


def test_ticket_flow_status_outputs_human_readable_status(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    _seed_ticket(repo_root)

    run_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.RUNNING)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip()
    assert f"Run id: {run_id}" in result.output
    assert "Status: running" in result.output


def test_ticket_flow_status_outputs_json_payload(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    _seed_ticket(repo_root)

    run_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _seed_repo_run(repo_root, run_id, FlowRunStatus.PAUSED)

    result = runner.invoke(
        app,
        [
            "flow",
            "ticket_flow",
            "status",
            "--repo",
            str(repo_root),
            "--run-id",
            run_id,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip()
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["status"] == "paused"
    assert payload["flow_type"] == "ticket_flow"
    assert "worker" in payload
    assert "ticket_progress" in payload
