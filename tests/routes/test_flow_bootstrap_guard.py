from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.routes import flows as flow_routes
from codex_autorunner.surfaces.web.routes.flows import FlowRoutesState


def _reset_state() -> None:
    pass


def test_bootstrap_reuses_active_run_with_hint(tmp_path, monkeypatch):
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    run_id = str(uuid.uuid4())
    record = store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={},
        metadata={},
        state={},
        current_step="bootstrap",
    )
    assert record.id == run_id
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
    store.close()

    artifacts_dir = tmp_path / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    health = FlowWorkerHealth(
        status="alive",
        pid=1234,
        cmdline=[
            "python",
            "-m",
            "codex_autorunner",
            "flow",
            "worker",
            "--run-id",
            run_id,
        ],
        artifact_path=artifacts_dir / "worker.json",
        message=None,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health",
        lambda *a, **k: health,
    )

    spawned = {"count": 0}

    def fake_start_worker(*_args, **_kwargs):
        spawned["count"] += 1
        return None

    monkeypatch.setattr(flow_routes, "_start_flow_worker", fake_start_worker)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post("/api/flows/ticket_flow/bootstrap", json={})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == run_id
    assert payload["state"]["hint"] == "active_run_reused"
    assert spawned["count"] == 1


def test_bootstrap_honors_force_new(tmp_path, monkeypatch):
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    existing = store.create_flow_run(
        run_id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        input_data={},
        metadata={},
        state={},
        current_step="bootstrap",
    )
    store.update_flow_run_status(existing.id, FlowRunStatus.RUNNING)
    store.close()

    store = FlowStore(db_path)
    store.initialize()

    class StubController:
        def __init__(self, backing_store: FlowStore):
            self.store = backing_store

        def list_runs(self, status=None):
            return self.store.list_flow_runs(flow_type="ticket_flow", status=status)

        async def start_flow(self, input_data, run_id, metadata=None):
            return self.store.create_flow_run(
                run_id=run_id,
                flow_type="ticket_flow",
                input_data=input_data or {},
                metadata=metadata or {},
                state={},
                current_step="bootstrap",
            )

    monkeypatch.setattr(
        flow_routes,
        "_get_flow_controller",
        lambda _repo_root, _flow_type, _state: StubController(store),
    )
    monkeypatch.setattr(flow_routes, "_start_flow_worker", lambda *_, **__: None)
    monkeypatch.setattr(
        flow_routes,
        "check_worker_health",
        lambda *_, **__: FlowWorkerHealth(  # type: ignore[arg-type]
            status="dead",
            pid=None,
            cmdline=[],
            artifact_path=tmp_path
            / ".codex-autorunner"
            / "flows"
            / "dummy"
            / "worker.json",
            message=None,
        ),
    )

    # Force new should ignore the existing run and create a new one.
    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/bootstrap",
            json={"metadata": {"force_new": True}},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] != existing.id
    assert payload.get("state", {}).get("hint") is None


def test_bootstrap_skips_seeding_when_tickets_exist(tmp_path, monkeypatch):
    """Bootstrap should not create a new ticket when any ticket already exists."""

    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    # Simulate an existing ticket with a non-001 index
    (ticket_dir / "TICKET-010.md").write_text(
        "--\nagent: codex\ndone: false\n--\n", encoding="utf-8"
    )

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    class StubController:
        def __init__(self, backing_store: FlowStore):
            self.store = backing_store

        async def start_flow(self, input_data, run_id, metadata=None):
            return self.store.create_flow_run(
                run_id=run_id,
                flow_type="ticket_flow",
                input_data=input_data or {},
                metadata=metadata or {},
                state={},
                current_step="bootstrap",
            )

    monkeypatch.setattr(
        flow_routes,
        "_get_flow_controller",
        lambda _repo_root, _flow_type, _state: StubController(store),
    )
    monkeypatch.setattr(flow_routes, "_start_flow_worker", lambda *_, **__: None)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post("/api/flows/ticket_flow/bootstrap", json={})

    assert resp.status_code == 200
    # Should not seed TICKET-001 because tickets already exist
    assert not (ticket_dir / "TICKET-001.md").exists()


def test_start_flow_worker_skips_when_process_alive(tmp_path, monkeypatch):
    _reset_state()

    repo_root = Path(tmp_path)
    run_id = str(uuid.uuid4())
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    health = FlowWorkerHealth(
        status="alive",
        pid=4321,
        cmdline=[
            "python",
            "-m",
            "codex_autorunner",
            "flow",
            "worker",
            "--run-id",
            run_id,
        ],
        artifact_path=artifacts_dir / "worker.json",
        message=None,
    )
    monkeypatch.setattr(
        flow_routes,
        "ensure_worker",
        lambda *_args, **_kwargs: {"status": "reused", "health": health},
    )

    state = FlowRoutesState()
    proc = flow_routes._start_flow_worker(repo_root, run_id, state)

    assert proc is None


def test_ticket_flow_start_rejects_no_tickets(tmp_path, monkeypatch):
    """Starting ticket_flow with force_new should fail when no tickets exist."""
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    class StubController:
        def __init__(self, backing_store: FlowStore):
            self.store = backing_store

        async def start_flow(self, input_data, run_id, metadata=None):
            return self.store.create_flow_run(
                run_id=run_id,
                flow_type="ticket_flow",
                input_data=input_data or {},
                metadata=metadata or {},
                state={},
                current_step="ticket_turn",
            )

    monkeypatch.setattr(
        flow_routes,
        "_get_flow_controller",
        lambda _repo_root, _flow_type, _state: StubController(store),
    )

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/start",
            json={"metadata": {"force_new": True}},
        )

    assert resp.status_code == 400
    payload = resp.json()
    assert "detail" in payload
    assert "No tickets found" in payload["detail"]
    assert ".codex-autorunner/tickets" in payload["detail"]
    assert "/api/flows/ticket_flow/bootstrap" in payload["detail"]


def test_ticket_flow_start_allows_with_tickets(tmp_path, monkeypatch):
    """Starting ticket_flow with force_new should succeed when tickets exist."""
    _reset_state()
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---\n", encoding="utf-8"
    )

    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    store = FlowStore(db_path)
    store.initialize()

    class StubController:
        def __init__(self, backing_store: FlowStore):
            self.store = backing_store

        async def start_flow(self, input_data, run_id, metadata=None):
            return self.store.create_flow_run(
                run_id=run_id,
                flow_type="ticket_flow",
                input_data=input_data or {},
                metadata=metadata or {},
                state={},
                current_step="ticket_turn",
            )

    monkeypatch.setattr(
        flow_routes,
        "_get_flow_controller",
        lambda _repo_root, _flow_type, _state: StubController(store),
    )
    monkeypatch.setattr(flow_routes, "_start_flow_worker", lambda *_, **__: None)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        resp = client.post(
            "/api/flows/ticket_flow/start",
            json={"metadata": {"force_new": True}},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert "id" in payload
