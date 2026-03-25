from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.github.service import GitHubService, RepoInfo


def test_discover_pr_binding_summary_for_branch_returns_normalized_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    seen: dict[str, object] = {}

    def _fake_pr_for_branch(
        *, branch: str, cwd: Path | None = None
    ) -> dict[str, object]:
        seen["branch"] = branch
        seen["cwd"] = cwd
        return {
            "number": "17",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "feature/login",
            "baseRefName": "main",
            "url": "https://github.com/acme/widgets/pull/17",
            "title": "Login flow",
        }

    monkeypatch.setattr(service, "pr_for_branch", _fake_pr_for_branch)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )

    summary = service.discover_pr_binding_summary(
        branch="feature/login", cwd=tmp_path / "repo"
    )

    assert summary == {
        "repo_slug": "acme/widgets",
        "pr_number": 17,
        "pr_state": "open",
        "head_branch": "feature/login",
        "base_branch": "main",
    }
    assert seen == {"branch": "feature/login", "cwd": tmp_path / "repo"}


def test_discover_pr_binding_summary_uses_current_branch_when_unspecified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    monkeypatch.setattr(service, "current_branch", lambda *, cwd=None: "feature/draft")
    monkeypatch.setattr(
        service,
        "pr_for_branch",
        lambda *, branch, cwd=None: {
            "number": 23,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": branch,
        },
    )
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )

    summary = service.discover_pr_binding_summary()

    assert summary == {
        "repo_slug": "acme/widgets",
        "pr_number": 23,
        "pr_state": "draft",
        "head_branch": "feature/draft",
    }


def test_discover_pr_binding_summary_returns_none_when_branch_has_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    monkeypatch.setattr(
        service, "current_branch", lambda *, cwd=None: "feature/missing"
    )
    monkeypatch.setattr(service, "pr_for_branch", lambda *, branch, cwd=None: None)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: pytest.fail("repo_info should not be called when no PR is found"),
    )

    assert service.discover_pr_binding_summary() is None
