from __future__ import annotations

import logging
from pathlib import Path

from codex_autorunner.surfaces.web.routes.flow_routes.dependencies import (
    build_default_flow_route_dependencies,
)


def test_seed_issue_dependency_adapter_uses_helper_signatures(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    observed: dict[str, object] = {}

    def _fake_seed_issue_from_github(
        repo_root_arg: Path,
        issue_ref: str,
        github_service_factory=None,  # noqa: ANN001
    ) -> str:
        observed["github"] = (repo_root_arg, issue_ref, github_service_factory)
        return "github-seed"

    def _fake_seed_issue_from_text(plan_text: str) -> str:
        observed["text"] = plan_text
        return "text-seed"

    monkeypatch.setattr(
        "codex_autorunner.core.flows.ux_helpers.seed_issue_from_github",
        _fake_seed_issue_from_github,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.ux_helpers.seed_issue_from_text",
        _fake_seed_issue_from_text,
    )

    deps = build_default_flow_route_dependencies()
    github_factory = object()

    assert (
        deps.seed_issue(
            repo_root,
            "run-1",
            issue_url="#7",
            github_service_factory=github_factory,
        )
        == "github-seed"
    )
    assert observed["github"] == (repo_root, "#7", github_factory)

    assert deps.seed_issue(repo_root, "run-1", issue_text="write a plan") == "text-seed"
    assert observed["text"] == "write a plan"


def test_safe_list_flow_runs_dependency_adapter_uses_flow_store_service(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    observed: dict[str, object] = {}

    def _fake_safe_list_flow_runs(
        repo_root_arg: Path,
        flow_type: str | None = None,
        *,
        recover_stuck: bool = False,
        logger: logging.Logger | None = None,
    ) -> list[str]:
        observed["call"] = (repo_root_arg, flow_type, recover_stuck, logger)
        return ["run-1"]

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.services.flow_store.safe_list_flow_runs",
        _fake_safe_list_flow_runs,
    )

    deps = build_default_flow_route_dependencies()

    assert deps.safe_list_flow_runs(
        repo_root,
        flow_type="ticket_flow",
        recover_stuck=True,
    ) == ["run-1"]
    repo_root_arg, flow_type, recover_stuck, logger = observed["call"]
    assert repo_root_arg == repo_root
    assert flow_type == "ticket_flow"
    assert recover_stuck is True
    assert isinstance(logger, logging.Logger)
    assert logger.name == (
        "codex_autorunner.surfaces.web.routes.flow_routes.dependencies"
    )


def test_get_flow_controller_dependency_adapter_imports_core_controller(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    state = object()
    observed: dict[str, object] = {}
    sentinel = object()

    def _fake_get_flow_controller(
        repo_root_arg: Path, flow_type: str, state_arg: object
    ) -> object:
        observed["call"] = (repo_root_arg, flow_type, state_arg)
        return sentinel

    monkeypatch.setattr(
        "codex_autorunner.surfaces.web.routes.flow_routes.definitions.get_flow_controller",
        _fake_get_flow_controller,
    )

    deps = build_default_flow_route_dependencies()

    assert deps.get_flow_controller(repo_root, "ticket_flow", state) is sentinel
    assert observed["call"] == (repo_root, "ticket_flow", state)
