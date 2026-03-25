from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.publish_executor import TerminalPublishError
from codex_autorunner.integrations.github.publisher import (
    build_post_pr_comment_executor,
    publish_pr_comment,
)
from codex_autorunner.integrations.github.service import RepoInfo


class _FakeGitHubService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def repo_info(self) -> RepoInfo:
        return RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        )

    def create_issue_comment(
        self, *, owner: str, repo: str, number: int, body: str, cwd: Path | None = None
    ) -> dict[str, object]:
        self.calls.append(
            {
                "owner": owner,
                "repo": repo,
                "number": number,
                "body": body,
                "cwd": cwd,
            }
        )
        return {
            "id": 1234,
            "html_url": f"https://github.com/{owner}/{repo}/pull/{number}#issuecomment-1234",
        }


def test_publish_pr_comment_supports_explicit_repo_slug_and_pr_number() -> None:
    service = _FakeGitHubService()

    result = publish_pr_comment(
        {
            "repo_slug": "octo/repo",
            "pr_number": 42,
            "body": "Looks good.",
        },
        service=service,
    )

    assert result == {
        "repo_slug": "octo/repo",
        "pr_number": 42,
        "comment_id": 1234,
        "url": "https://github.com/octo/repo/pull/42#issuecomment-1234",
    }
    assert service.calls == [
        {
            "owner": "octo",
            "repo": "repo",
            "number": 42,
            "body": "Looks good.",
            "cwd": None,
        }
    ]


def test_post_pr_comment_executor_uses_service_repo_for_numeric_pr_ref(
    tmp_path: Path,
) -> None:
    service = _FakeGitHubService()
    service_roots: list[Path] = []

    def _factory(repo_root: Path, raw_config=None) -> _FakeGitHubService:
        _ = raw_config
        service_roots.append(repo_root)
        return service

    operation = type(
        "Operation",
        (),
        {
            "payload": {"pr_ref": "17", "body": "Queue this note."},
            "operation_kind": "post_pr_comment",
            "operation_key": "comment:17",
        },
    )()

    executor = build_post_pr_comment_executor(
        repo_root=tmp_path,
        github_service_factory=_factory,
    )
    result = executor(operation)

    assert service_roots == [tmp_path]
    assert result == {
        "repo_slug": "acme/widgets",
        "pr_number": 17,
        "comment_id": 1234,
        "url": "https://github.com/acme/widgets/pull/17#issuecomment-1234",
    }
    assert service.calls == [
        {
            "owner": "acme",
            "repo": "widgets",
            "number": 17,
            "body": "Queue this note.",
            "cwd": tmp_path,
        }
    ]


def test_publish_pr_comment_requires_body() -> None:
    service = _FakeGitHubService()

    with pytest.raises(TerminalPublishError, match="body"):
        publish_pr_comment(
            {"repo_slug": "acme/widgets", "pr_number": 9}, service=service
        )
