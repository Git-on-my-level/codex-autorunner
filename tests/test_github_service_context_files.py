from __future__ import annotations

import string
from pathlib import Path

import pytest

from codex_autorunner.integrations.github.service import GitHubService, RepoInfo


def _configure_service(monkeypatch: pytest.MonkeyPatch, service: GitHubService) -> None:
    monkeypatch.setattr(service, "gh_available", lambda: True)
    monkeypatch.setattr(service, "gh_authenticated", lambda: True)


def test_cross_repo_link_rejected_when_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    _configure_service(monkeypatch, service)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="local/repo",
            url="https://github.com/local/repo",
            default_branch="main",
        ),
    )

    result = service.build_context_file_from_url(
        "https://github.com/other/repo/issues/42",
        allow_cross_repo=False,
    )
    assert result is None


def test_cross_repo_issue_context_is_namespaced_by_repo_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    _configure_service(monkeypatch, service)
    monkeypatch.setattr(
        service,
        "issue_view",
        lambda *, number, cwd=None, repo_slug=None: {
            "number": number,
            "url": f"https://github.com/{repo_slug}/issues/{number}",
            "title": "Issue title",
            "body": "Issue body",
            "state": "OPEN",
            "author": {"login": "tester"},
            "labels": [],
            "comments": [],
        },
    )

    result = service.build_context_file_from_url(
        "https://github.com/Org-One/Repo_A/issues/42",
        allow_cross_repo=True,
    )
    assert result is not None
    repo_dir = Path(result["path"]).parent.name
    assert repo_dir.startswith("org-one--repo_a-")
    hash_part = repo_dir.removeprefix("org-one--repo_a-")
    assert len(hash_part) == 10
    assert all(ch in string.hexdigits for ch in hash_part)
    assert result["path"].endswith("/issue-42.md")
    assert (tmp_path / result["path"]).exists()


def test_cross_repo_pr_context_paths_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    _configure_service(monkeypatch, service)
    monkeypatch.setattr(
        service,
        "pr_view",
        lambda *, number, cwd=None, repo_slug=None: {
            "number": number,
            "url": f"https://github.com/{repo_slug}/pull/{number}",
            "title": "PR title",
            "body": "PR body",
            "state": "OPEN",
            "author": {"login": "tester"},
            "labels": [],
            "files": [],
            "additions": 1,
            "deletions": 1,
            "changedFiles": 1,
            "headRefName": "feature",
            "baseRefName": "main",
        },
    )
    monkeypatch.setattr(
        service,
        "pr_review_threads",
        lambda *, owner, repo, number, cwd=None: [],
    )

    first = service.build_context_file_from_url(
        "https://github.com/acme/service-a/pull/99",
        allow_cross_repo=True,
    )
    second = service.build_context_file_from_url(
        "https://github.com/acme/service-b/pull/99",
        allow_cross_repo=True,
    )

    assert first is not None
    assert second is not None
    assert first["path"] != second["path"]
    assert (tmp_path / first["path"]).exists()
    assert (tmp_path / second["path"]).exists()


def test_cross_repo_slug_ambiguities_are_collision_resistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    _configure_service(monkeypatch, service)
    monkeypatch.setattr(
        service,
        "issue_view",
        lambda *, number, cwd=None, repo_slug=None: {
            "number": number,
            "url": f"https://github.com/{repo_slug}/issues/{number}",
            "title": "Issue title",
            "body": "Issue body",
            "state": "OPEN",
            "author": {"login": "tester"},
            "labels": [],
            "comments": [],
        },
    )

    first = service.build_context_file_from_url(
        "https://github.com/acme--ops/api/issues/12",
        allow_cross_repo=True,
    )
    second = service.build_context_file_from_url(
        "https://github.com/acme/ops--api/issues/12",
        allow_cross_repo=True,
    )

    assert first is not None
    assert second is not None
    assert first["path"] != second["path"]
    assert (tmp_path / first["path"]).exists()
    assert (tmp_path / second["path"]).exists()
