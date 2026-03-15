from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.flow_routes.dependencies import (
    FlowRouteDependencies,
)
from codex_autorunner.surfaces.web.routes.flow_routes.ticket_bootstrap import (
    bootstrap_check,
    build_ticket_bootstrap_routes,
    seed_issue,
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


def test_extracted_bootstrap_helpers_match_ux_helper_contracts(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    observed: dict[str, object] = {}

    class _BootstrapResult:
        status = "needs_issue"
        github_available = False
        repo_slug = None

    def _fake_bootstrap_check(
        repo_root_arg: Path, github_service_factory=None  # noqa: ANN001
    ):
        observed["bootstrap"] = (repo_root_arg, github_service_factory)
        return _BootstrapResult()

    def _fake_seed_issue_from_text(plan_text: str) -> str:
        observed["plan_text"] = plan_text
        return "seeded-text"

    monkeypatch.setattr(
        "codex_autorunner.core.flows.ux_helpers.bootstrap_check",
        _fake_bootstrap_check,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.ux_helpers.seed_issue_from_text",
        _fake_seed_issue_from_text,
    )

    result = bootstrap_check(repo_root, state=None)  # type: ignore[arg-type]
    assert result.status == "needs_issue"
    assert observed["bootstrap"] == (repo_root, None)

    seeded = seed_issue(
        repo_root,
        state=None,  # type: ignore[arg-type]
        body={
            "source": "text",
            "title": "Fix queue bug",
            "body": "Make the queue surface actionable work.",
            "labels": ["bug", "p1"],
            "assignees": ["codex"],
        },
    )
    assert seeded == "seeded-text"
    assert observed["plan_text"] == (
        "Title: Fix queue bug\n\n"
        "Make the queue surface actionable work.\n\n"
        "Labels: bug, p1\n"
        "Assignees: codex"
    )
