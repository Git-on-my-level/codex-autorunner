import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows import (
    FlowDefinition,
    FlowEventType,
    FlowRunStatus,
    StepOutcome,
)
from codex_autorunner.routes import flows as flow_routes


async def _simple_step(record, input_data):
    return StepOutcome.complete(output={"ok": True})


def test_flow_route_runs_worker_and_completes(tmp_path, monkeypatch):
    flow_routes._controller_cache.clear()
    flow_routes._definition_cache.clear()

    definition = FlowDefinition(
        flow_type="ticket_flow", initial_step="step1", steps={"step1": _simple_step}
    )
    definition.validate()

    monkeypatch.setattr(
        flow_routes, "_build_flow_definition", lambda repo_root, flow_type: definition
    )
    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: Path(tmp_path))

    def _fake_start_worker(repo_root: Path, run_id: str):
        # Run synchronously in tests; actual worker is handled by a subprocess.
        return None

    monkeypatch.setattr(flow_routes, "_start_flow_worker", _fake_start_worker)

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    try:
        with TestClient(app) as client:
            resp = client.post("/api/flows/ticket_flow/start", json={"input_data": {}})
            assert resp.status_code == 200
            run_id = resp.json()["id"]

            controller = flow_routes._get_flow_controller(
                Path(tmp_path), definition.flow_type
            )

            asyncio.run(controller.run_flow(run_id))
            status = client.get(f"/api/flows/{run_id}/status").json()

            events = controller.get_events(run_id)
            assert any(evt.event_type == FlowEventType.FLOW_COMPLETED for evt in events)
            assert status["status"] == FlowRunStatus.COMPLETED.value
    finally:
        controller = flow_routes._controller_cache.get(
            (Path(tmp_path).resolve(), definition.flow_type)
        )
        if controller:
            controller.shutdown()
        flow_routes._controller_cache.clear()
        flow_routes._definition_cache.clear()
        flow_routes._active_workers.clear()
