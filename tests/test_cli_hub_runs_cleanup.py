from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

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


def test_hub_runs_cleanup_archives_and_deletes_terminal_runs(hub_env) -> None:
    run_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    _seed_repo_run(hub_env.repo_root, run_id, FlowRunStatus.STOPPED)

    run_dir = hub_env.repo_root / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "hub",
            "runs",
            "cleanup",
            "--path",
            str(hub_env.hub_root),
            "--stale",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["errors"] == []
    assert len(payload["results"]) == 1
    entry = payload["results"][0]
    assert entry["repo_id"] == hub_env.repo_id
    assert entry["run_id"] == run_id
    assert entry["archived_runs"] is True
    assert entry["deleted_run"] is True

    archived_root = (
        hub_env.repo_root / ".codex-autorunner" / "flows" / run_id / "archived_runs"
    )
    assert archived_root.exists()

    db_path = hub_env.repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        assert store.get_flow_run(run_id) is None
