from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.flows import FlowRunStatus
from codex_autorunner.surfaces.web.routes import flows as flow_routes
from codex_autorunner.surfaces.web.routes.pma_routes import (
    action_manifest as pma_actions,
)


def test_ticket_flow_action_manifest_route_shape(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\ntitle: "One"\nagent: codex\ndone: false\n---\n\nBody\n',
        encoding="utf-8",
    )
    record = SimpleNamespace(id="run-1", status=FlowRunStatus.RUNNING)

    monkeypatch.setattr(flow_routes, "find_repo_root", lambda: repo_root)
    monkeypatch.setattr(
        flow_routes,
        "_safe_list_flow_runs",
        lambda *_args, **_kwargs: [record],
    )
    monkeypatch.setattr(
        flow_routes,
        "check_worker_health",
        lambda *_args, **_kwargs: SimpleNamespace(status="alive"),
    )
    monkeypatch.setattr(
        flow_routes,
        "resolve_ticket_flow_archive_mode",
        lambda _record: "blocked",
    )

    app = FastAPI()
    app.include_router(flow_routes.build_flow_routes())

    with TestClient(app) as client:
        response = client.get(
            "/api/flows/ticket_flow/action-manifest?resource_kind=repo&resource_id=repo-1"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "surface-action-manifest-v1"
    assert payload["target_kind"] == "ticket_flow"
    stop = next(
        action
        for action in payload["actions"]
        if action["action_id"] == "ticket_flow.stop"
    )
    assert stop["enabled"] is True
    assert stop["route"] == "/api/flows/run-1/stop"


def test_pma_thread_action_manifest_route_shape(monkeypatch) -> None:
    class Store:
        def get_thread(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return {
                "thread_target_id": "thread-1",
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": "repo-1",
            }

        def get_running_turn(self, managed_thread_id: str):
            assert managed_thread_id == "thread-1"
            return {"managed_turn_id": "turn-1", "status": "running"}

    monkeypatch.setattr(
        pma_actions,
        "get_pma_request_context",
        lambda _request: SimpleNamespace(thread_store=lambda: Store()),
    )
    monkeypatch.setattr(
        pma_actions,
        "_thread_capabilities",
        lambda _request, _thread: frozenset({"interrupt"}),
    )

    app = FastAPI()
    router = APIRouter(prefix="/hub/pma")
    pma_actions.build_action_manifest_routes(router)
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/hub/pma/threads/thread-1/action-manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["target_kind"] == "managed_thread"
    interrupt = payload["actions"][0]
    assert interrupt["action_id"] == "managed_thread.interrupt"
    assert interrupt["enabled"] is True
    assert interrupt["command_id"] == "car.interrupt"
