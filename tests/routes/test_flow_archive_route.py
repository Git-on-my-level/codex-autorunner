from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.surfaces.web.routes import flows as flows_route_module
from codex_autorunner.web.app import create_repo_app


def _client_for_repo(repo_root: Path) -> TestClient:
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    return TestClient(create_repo_app(repo_root))


def _create_run(repo_root: Path, run_id: str, status: FlowRunStatus) -> None:
    with FlowStore(repo_root / ".codex-autorunner" / "flows.db") as store:
        store.initialize()
        store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, status)


def test_archive_route_deletes_run_record_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)
    run_id = str(uuid.uuid4())
    _create_run(repo_root, run_id, FlowRunStatus.COMPLETED)
    captured: list[dict[str, Any]] = []

    def _archive_flow_run_artifacts(
        repo_root_arg: Path, **kwargs: Any
    ) -> dict[str, Any]:
        captured.append({"repo_root": str(repo_root_arg), **kwargs})
        return {
            "run_id": kwargs["run_id"],
            "archived_tickets": 0,
            "archived_runs": True,
            "archived_contextspace": False,
            "missing_paths": [],
        }

    monkeypatch.setattr(
        flows_route_module,
        "archive_flow_run_artifacts",
        _archive_flow_run_artifacts,
    )

    response = client.post(f"/api/flows/{run_id}/archive")

    assert response.status_code == 200
    assert captured == [
        {
            "repo_root": str(repo_root),
            "run_id": run_id,
            "force": False,
            "delete_run": True,
        }
    ]
