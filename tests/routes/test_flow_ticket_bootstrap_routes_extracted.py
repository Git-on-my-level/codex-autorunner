from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.flow_routes.dependencies import (
    FlowRouteDependencies,
)
from codex_autorunner.surfaces.web.routes.flow_routes.ticket_bootstrap import (
    build_ticket_bootstrap_routes,
)


def test_extracted_bootstrap_route_loads_ticket_paths_without_import_error(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    deps = FlowRouteDependencies(
        find_repo_root=lambda: repo_root,
        build_flow_orchestration_service=lambda *_args, **_kwargs: None,
        require_flow_store=lambda _repo_root: None,
        safe_list_flow_runs=lambda *args, **kwargs: [],
        build_flow_status_response=lambda *args, **kwargs: {},
        get_flow_record=lambda *args, **kwargs: None,
        get_flow_controller=lambda *args, **kwargs: None,
        start_flow_worker=lambda *args, **kwargs: None,
        recover_flow_store_if_possible=lambda *args, **kwargs: None,
        bootstrap_check=lambda *args, **kwargs: None,
        seed_issue=lambda *args, **kwargs: {},
    )

    router, _ = build_ticket_bootstrap_routes(deps)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post("/api/flows/ticket_flow/bootstrap", json={})

    assert response.status_code == 400
    assert "Bootstrap not fully implemented" in response.json()["detail"]
    assert (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
