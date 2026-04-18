from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.core.publish_executor import TerminalPublishError
from codex_autorunner.integrations.github.publisher import (
    build_post_pr_comment_executor,
    build_react_pr_review_comment_executor,
    publish_pr_comment,
    publish_pr_review_comment_reaction,
)
from codex_autorunner.integrations.github.service import GitHubError, RepoInfo


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


class _FailingGitHubService:
    def __init__(self, *, error: Exception) -> None:
        self._error = error
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
            {"owner": owner, "repo": repo, "number": number, "body": body}
        )
        raise self._error

    def create_pull_request_review_comment_reaction(
        self,
        *,
        owner: str,
        repo: str,
        comment_id: int,
        content: str,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {"owner": owner, "repo": repo, "comment_id": comment_id, "content": content}
        )
        raise self._error


class TestPublisherBoundaryNoRetryState:
    def test_post_pr_comment_executor_wraps_github_error_as_terminal(
        self, tmp_path: Path
    ) -> None:
        service = _FailingGitHubService(
            error=GitHubError("API rate limit exceeded", status_code=429)
        )

        def _factory(repo_root: Path, raw_config: Any = None) -> _FailingGitHubService:
            return service

        operation = type(
            "Operation",
            (),
            {
                "payload": {
                    "repo_slug": "acme/widgets",
                    "pr_number": 1,
                    "body": "hello",
                },
                "operation_kind": "post_pr_comment",
                "operation_key": "comment:1",
            },
        )()

        executor = build_post_pr_comment_executor(
            repo_root=tmp_path, github_service_factory=_factory
        )

        with pytest.raises(TerminalPublishError, match="rate limit"):
            executor(operation)

        assert len(service.calls) == 1

    def test_post_pr_comment_executor_does_not_retry_on_failure(
        self, tmp_path: Path
    ) -> None:
        call_count = {"n": 0}

        class _CountingFailService:
            def repo_info(self) -> RepoInfo:
                return RepoInfo(
                    name_with_owner="acme/widgets",
                    url="https://github.com/acme/widgets",
                    default_branch="main",
                )

            def create_issue_comment(
                self,
                *,
                owner: str,
                repo: str,
                number: int,
                body: str,
                cwd: Path | None = None,
            ) -> dict[str, object]:
                call_count["n"] += 1
                raise GitHubError("internal error", status_code=500)

        def _factory(repo_root: Path, raw_config: Any = None) -> _CountingFailService:
            return _CountingFailService()

        operation = type(
            "Operation",
            (),
            {
                "payload": {
                    "repo_slug": "acme/widgets",
                    "pr_number": 1,
                    "body": "hello",
                },
                "operation_kind": "post_pr_comment",
                "operation_key": "comment:1",
            },
        )()

        executor = build_post_pr_comment_executor(
            repo_root=tmp_path, github_service_factory=_factory
        )

        with pytest.raises(TerminalPublishError):
            executor(operation)

        assert call_count["n"] == 1

    def test_react_executor_wraps_github_error_as_terminal(
        self, tmp_path: Path
    ) -> None:
        service = _FailingGitHubService(error=GitHubError("forbidden", status_code=403))

        def _factory(repo_root: Path, raw_config: Any = None) -> _FailingGitHubService:
            return service

        operation = type(
            "Operation",
            (),
            {
                "payload": {"repo_slug": "acme/widgets", "comment_id": 42},
                "operation_kind": "react_pr_review_comment",
                "operation_key": "react:42",
            },
        )()

        executor = build_react_pr_review_comment_executor(
            repo_root=tmp_path, github_service_factory=_factory
        )

        with pytest.raises(TerminalPublishError, match="forbidden"):
            executor(operation)

        assert len(service.calls) == 1

    def test_publish_pr_comment_missing_repo_and_pr_raises_terminal(self) -> None:
        service = _FakeGitHubService()

        with pytest.raises(TerminalPublishError, match="repo_slug"):
            publish_pr_comment({"body": "hello"}, service=service)

    def test_react_pr_review_comment_missing_comment_id_raises_terminal(self) -> None:
        service = _FakeGitHubService()

        with pytest.raises(TerminalPublishError, match="comment_id"):
            publish_pr_review_comment_reaction(
                {"repo_slug": "acme/widgets"}, service=service
            )
