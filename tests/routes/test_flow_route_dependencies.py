from __future__ import annotations

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
