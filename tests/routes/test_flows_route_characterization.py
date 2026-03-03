from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes import flows as flow_routes


def test_list_runs_falls_back_to_safe_listing_when_store_unavailable(
    tmp_path, monkeypatch
):
    repo_root = Path(tmp_path)
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: repo_root)
    monkeypatch.setattr(flow_routes, "_require_flow_store", lambda _repo_root: None)

    observed: dict[str, object] = {}

    def fake_safe_list_runs(
        root: Path, flow_type: str | None = None, *, recover_stuck: bool = False
    ):
        observed["root"] = root
        observed["flow_type"] = flow_type
        observed["recover_stuck"] = recover_stuck
        return []

    monkeypatch.setattr(flow_routes, "_safe_list_flow_runs", fake_safe_list_runs)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")

    assert resp.status_code == 200
    assert resp.json() == []
    assert observed == {
        "root": repo_root,
        "flow_type": "ticket_flow",
        "recover_stuck": False,
    }


def test_list_runs_forwards_reconcile_to_fallback_safe_listing(tmp_path, monkeypatch):
    repo_root = Path(tmp_path)
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: repo_root)
    monkeypatch.setattr(flow_routes, "_require_flow_store", lambda _repo_root: None)

    observed: dict[str, object] = {}

    def fake_safe_list_runs(
        root: Path, flow_type: str | None = None, *, recover_stuck: bool = False
    ):
        observed["root"] = root
        observed["flow_type"] = flow_type
        observed["recover_stuck"] = recover_stuck
        return []

    monkeypatch.setattr(flow_routes, "_safe_list_flow_runs", fake_safe_list_runs)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow&reconcile=true")

    assert resp.status_code == 200
    assert resp.json() == []
    assert observed["root"] == repo_root
    assert observed["flow_type"] == "ticket_flow"
    assert observed["recover_stuck"] is True


def test_list_runs_closes_primary_store_and_passes_it_to_status_builder(
    tmp_path, monkeypatch
):
    repo_root = Path(tmp_path)
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: repo_root)

    class StubStore:
        def __init__(self) -> None:
            self.close_calls = 0
            self.record = object()

        def list_flow_runs(self, flow_type=None):  # noqa: ANN001
            assert flow_type == "ticket_flow"
            return [self.record]

        def close(self) -> None:
            self.close_calls += 1

    store = StubStore()
    monkeypatch.setattr(flow_routes, "_require_flow_store", lambda _repo_root: store)

    observed: dict[str, object] = {}

    def fake_status_builder(record, root: Path, *, store=None):  # noqa: ANN001
        observed["record"] = record
        observed["root"] = root
        observed["store"] = store
        return flow_routes.FlowStatusResponse(
            id="run-1",
            flow_type="ticket_flow",
            status="running",
            current_step=None,
            created_at="2026-01-01T00:00:00Z",
            started_at=None,
            finished_at=None,
            error_message=None,
            state={},
        )

    monkeypatch.setattr(flow_routes, "_build_flow_status_response", fake_status_builder)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.get("/api/flows/runs?flow_type=ticket_flow")

    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "run-1"
    assert observed["record"] is store.record
    assert observed["root"] == repo_root
    assert observed["store"] is store
    assert store.close_calls == 1


def test_sync_current_ticket_paths_closes_store_after_internal_error(
    tmp_path, monkeypatch
):
    class FailingStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def list_flow_runs(self, flow_type=None):  # noqa: ANN001
            raise RuntimeError("boom")

        def close(self) -> None:
            self.close_calls += 1

    store = FailingStore()
    monkeypatch.setattr(flow_routes, "_require_flow_store", lambda _repo_root: store)

    flow_routes._sync_active_run_current_ticket_paths_after_reorder(
        Path(tmp_path),
        [(Path(tmp_path) / "TICKET-003.md", Path(tmp_path) / "TICKET-001.md")],
    )

    assert store.close_calls == 1


def test_get_flow_record_returns_503_for_sqlite_errors_and_closes_store(
    tmp_path, monkeypatch
):
    class BrokenStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def get_flow_run(self, _run_id: str):
            raise sqlite3.OperationalError("database is locked")

        def close(self) -> None:
            self.close_calls += 1

    store = BrokenStore()
    monkeypatch.setattr(flow_routes, "_require_flow_store", lambda _repo_root: store)

    with pytest.raises(HTTPException) as exc_info:
        flow_routes._get_flow_record(Path(tmp_path), "run-123")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Flows database unavailable"
    assert store.close_calls == 1
