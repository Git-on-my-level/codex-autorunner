"""Tests for flows routes state isolation."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.routes import flows as flows_routes


def _seed_paused_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
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
    store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def test_state_isolation_between_apps(tmp_path, monkeypatch):
    """Test that two FastAPI apps do not share state."""
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"

    _seed_paused_run(repo_root, run_id)
    monkeypatch.setattr(flows_routes, "find_repo_root", lambda: repo_root)

    # Create two separate FastAPI apps
    app1 = FastAPI()
    app2 = FastAPI()

    # Each app gets its own router instance
    router1 = flows_routes.build_flow_routes()
    router2 = flows_routes.build_flow_routes()

    app1.include_router(router1)
    app2.include_router(router2)

    # Make a request to trigger state initialization
    with TestClient(app1) as client1:
        resp = client1.get("/api/flows")
        assert resp.status_code == 200

    with TestClient(app2) as client2:
        resp = client2.get("/api/flows")
        assert resp.status_code == 200

    # Verify they have separate state objects (now initialized after requests)
    state1 = app1.state.flow_routes_state
    state2 = app2.state.flow_routes_state

    # State objects should be different
    assert state1 is not state2, "State should be isolated between apps"

    # Verify they have separate caches
    assert id(state1.controller_cache) != id(
        state2.controller_cache
    ), "Controller caches should be separate"
    assert id(state1.definition_cache) != id(
        state2.definition_cache
    ), "Definition caches should be separate"
    assert id(state1.active_workers) != id(
        state2.active_workers
    ), "Active workers dicts should be separate"

    # Both definition caches should be populated (from the requests we made)
    assert len(state1.definition_cache) > 0, "App1 definition cache should be populated"
    assert len(state2.definition_cache) > 0, "App2 definition cache should be populated"

    # Controller caches should still be empty (only populated when controller is needed)
    assert len(state1.controller_cache) == 0, "App1 controller cache should be empty"
    assert len(state2.controller_cache) == 0, "App2 controller cache should be empty"

    # The cached definition objects should be different instances
    cache1_keys = list(state1.definition_cache.keys())
    cache2_keys = list(state2.definition_cache.keys())
    assert cache1_keys == cache2_keys, "Both caches should have the same keys"
    assert (
        state1.definition_cache[cache1_keys[0]]
        is not state2.definition_cache[cache2_keys[0]]
    ), "Cached definitions should be different objects"
