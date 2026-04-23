from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.surfaces.web.app import create_repo_app
from codex_autorunner.surfaces.web.routes import flows as flows_route_module


@pytest.fixture(scope="module")
def _flow_archive_env(tmp_path_factory):
    repo_root = tmp_path_factory.mktemp("repo")
    seed_hub_files(repo_root.parent, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    app = create_repo_app(repo_root)
    yield TestClient(app), repo_root


def _create_run(repo_root: Path, run_id: str, status: FlowRunStatus) -> None:
    with FlowStore(repo_root / ".codex-autorunner" / "flows.db") as store:
        store.initialize()
        store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, status)


def _seed_ticket_state(repo_root: Path, run_id: str) -> None:
    tickets_dir = repo_root / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text("ticket", encoding="utf-8")

    context_dir = repo_root / ".codex-autorunner" / "contextspace"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "active_context.md").write_text("Active context\n", encoding="utf-8")
    (context_dir / "decisions.md").write_text("Decision log\n", encoding="utf-8")

    run_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "DISPATCH.md").write_text(
        "---\nmode: pause\n---\n\nhello\n", encoding="utf-8"
    )
    live_flow_dir = repo_root / ".codex-autorunner" / "flows" / run_id / "chat"
    live_flow_dir.mkdir(parents=True, exist_ok=True)
    (live_flow_dir / "outbound.jsonl").write_text("{}", encoding="utf-8")


def test_archive_route_deletes_run_record_by_default(
    _flow_archive_env, monkeypatch
) -> None:
    client, repo_root = _flow_archive_env
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


def test_archive_route_cleans_live_contextspace_after_archiving(
    _flow_archive_env,
) -> None:
    client, repo_root = _flow_archive_env
    run_id = str(uuid.uuid4())
    _create_run(repo_root, run_id, FlowRunStatus.COMPLETED)
    _seed_ticket_state(repo_root, run_id)

    response = client.post(f"/api/flows/{run_id}/archive")

    assert response.status_code == 200
    payload = response.json()
    assert payload["archived_contextspace"] is True
    assert payload["tickets_archived"] == 1

    archived_context = (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "contextspace"
        / "active_context.md"
    )
    assert archived_context.read_text(encoding="utf-8") == "Active context\n"
    assert (
        repo_root
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "flow_state"
        / "chat"
        / "outbound.jsonl"
    ).read_text(encoding="utf-8") == "{}"
    assert (
        repo_root / ".codex-autorunner" / "contextspace" / "active_context.md"
    ).read_text(encoding="utf-8") == ""
    assert (
        repo_root / ".codex-autorunner" / "contextspace" / "decisions.md"
    ).read_text(encoding="utf-8") == ""
    assert not (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert not (repo_root / ".codex-autorunner" / "flows" / run_id).exists()
    assert not (repo_root / ".codex-autorunner" / "runs" / run_id).exists()
