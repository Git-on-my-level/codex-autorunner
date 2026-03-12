from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.flow_routes.dependencies import (
    FlowRouteDependencies,
)
from codex_autorunner.surfaces.web.routes.flow_routes.status_history_routes import (
    build_status_history_routes,
)


@dataclass
class _FakeRecord:
    id: str
    flow_type: str = "ticket_flow"
    input_data: dict[str, Any] = field(default_factory=dict)


def _build_app(repo_root: Path, record: _FakeRecord) -> TestClient:
    deps = FlowRouteDependencies(
        find_repo_root=lambda: repo_root,
        require_flow_store=lambda _repo_root: None,
        safe_list_flow_runs=lambda *args, **kwargs: [],
        build_flow_status_response=lambda *args, **kwargs: {},
        get_flow_record=lambda _repo_root, _run_id: record,
        get_flow_controller=lambda *args, **kwargs: None,
        start_flow_worker=lambda *args, **kwargs: None,
        recover_flow_store_if_possible=lambda *args, **kwargs: None,
        bootstrap_check=lambda *args, **kwargs: None,
        seed_issue=lambda *args, **kwargs: {},
    )
    router, _ = build_status_history_routes(deps)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_extracted_dispatch_history_scopes_to_run_and_serializes_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    run_id = "11111111-1111-1111-1111-111111111111"
    history_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    history_dir.mkdir(parents=True)
    (history_dir / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Need input\n---\n\nPlease confirm.\n",
        encoding="utf-8",
    )
    (history_dir / "note.txt").write_text("attached\n", encoding="utf-8")

    record = _FakeRecord(
        id=run_id,
        input_data={
            "workspace_root": str(repo_root),
            "runs_dir": ".codex-autorunner/runs",
        },
    )

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.flow_routes.history_artifacts.get_diff_stats_by_dispatch_seq",
        lambda *_args, **_kwargs: {
            1: {"insertions": 2, "deletions": 1, "files_changed": 1}
        },
    )

    client = _build_app(repo_root, record)
    response = client.get(f"/api/flows/{run_id}/dispatch_history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert len(payload["history"]) == 1
    entry = payload["history"][0]
    assert entry["dispatch"]["mode"] == "pause"
    assert entry["dispatch"]["title"] == "Need input"
    assert entry["dispatch"]["body"] == "Please confirm."
    assert entry["dispatch"]["is_handoff"] is True
    assert entry["dispatch"]["diff_stats"] == {
        "insertions": 2,
        "deletions": 1,
        "files_changed": 1,
    }
    assert entry["attachments"][0]["path"].startswith(".codex-autorunner/runs/")
    assert entry["path"].startswith(".codex-autorunner/runs/")


def test_extracted_dispatch_file_reads_run_specific_history(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    run_id = "22222222-2222-2222-2222-222222222222"
    history_dir = (
        repo_root / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    history_dir.mkdir(parents=True)
    (history_dir / "DISPATCH.md").write_text(
        "---\nmode: notify\n---\n\nFYI\n",
        encoding="utf-8",
    )
    (history_dir / "note.txt").write_text("attached\n", encoding="utf-8")

    record = _FakeRecord(
        id=run_id,
        input_data={
            "workspace_root": str(repo_root),
            "runs_dir": ".codex-autorunner/runs",
        },
    )

    client = _build_app(repo_root, record)
    response = client.get(f"/api/flows/{run_id}/dispatch_history/0001/note.txt")

    assert response.status_code == 200
    assert response.text == "attached\n"


def test_extracted_status_history_routes_support_reconcile_requests(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    record = _FakeRecord(id="33333333-3333-3333-3333-333333333333")
    observed: dict[str, object] = {}

    class _Store:
        def __init__(self) -> None:
            self.close_calls = 0

        def list_flow_runs(self, flow_type=None):  # noqa: ANN001
            observed["flow_type"] = flow_type
            return [record]

        def close(self) -> None:
            self.close_calls += 1

    store = _Store()

    def _status_response(rec, _repo_root: Path, *, store=None):  # noqa: ANN001
        return {"id": rec.id, "store_present": store is not None}

    deps = FlowRouteDependencies(
        find_repo_root=lambda: repo_root,
        require_flow_store=lambda _repo_root: store,
        safe_list_flow_runs=lambda *args, **kwargs: [],
        build_flow_status_response=_status_response,
        get_flow_record=lambda _repo_root, _run_id: record,
        get_flow_controller=lambda *args, **kwargs: None,
        start_flow_worker=lambda *args, **kwargs: None,
        recover_flow_store_if_possible=lambda *args, **kwargs: None,
        bootstrap_check=lambda *args, **kwargs: None,
        seed_issue=lambda *args, **kwargs: {},
    )

    def _fake_reconcile(repo_root_arg, rec, store_arg, logger=None):  # noqa: ANN001
        observed["repo_root"] = repo_root_arg
        observed["record_id"] = rec.id
        observed["same_store"] = store_arg is store
        observed["logger"] = logger is not None
        return rec, False, False

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.reconcile_flow_run",
        _fake_reconcile,
    )

    router, _ = build_status_history_routes(deps)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/api/flows/runs?flow_type=ticket_flow&reconcile=true")

    assert response.status_code == 200
    assert response.json() == [{"id": record.id, "store_present": True}]
    assert observed == {
        "flow_type": "ticket_flow",
        "repo_root": repo_root,
        "record_id": record.id,
        "same_store": True,
        "logger": True,
    }
    assert store.close_calls == 1
