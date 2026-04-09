from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.integrations.github.service import GitHubService


def test_issue_comments_pages_through_all_pr_comment_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    page_calls: list[int] = []

    def _fake_gh(
        args: list[str], *, cwd=None, check=True, timeout_seconds=None
    ):  # type: ignore[no-untyped-def]
        _ = cwd, check, timeout_seconds
        assert args[:2] == ["api", "repos/acme/widgets/issues/17/comments"]
        page = int(
            next(arg.split("=", 1)[1] for arg in args if arg.startswith("page="))
        )
        per_page = int(
            next(arg.split("=", 1)[1] for arg in args if arg.startswith("per_page="))
        )
        page_calls.append(page)
        if page == 1:
            payload = [
                {
                    "id": index,
                    "body": f"comment {index}",
                    "html_url": f"https://example.invalid/comments/{index}",
                    "author_association": "MEMBER",
                    "updated_at": f"2026-03-30T00:{index:02d}:00Z",
                    "user": {"login": f"user-{index}", "type": "User"},
                }
                for index in range(1, per_page + 1)
            ]
        elif page == 2:
            payload = [
                {
                    "id": 101,
                    "body": "comment 101",
                    "html_url": "https://example.invalid/comments/101",
                    "author_association": "MEMBER",
                    "updated_at": "2026-03-30T01:41:00Z",
                    "user": {"login": "user-101", "type": "User"},
                }
            ]
        else:
            payload = []
        return type(
            "Proc",
            (),
            {"returncode": 0, "stdout": json.dumps(payload)},
        )()

    monkeypatch.setattr(service, "_gh", _fake_gh)

    comments = service.issue_comments(owner="acme", repo="widgets", number=17)

    assert page_calls == [1, 2]
    assert len(comments) == 101
    assert comments[0]["comment_id"] == "1"
    assert comments[-1]["comment_id"] == "101"
    assert comments[-1]["issue_number"] == 17


def test_pr_review_threads_prefers_numeric_database_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})

    def _fake_gh(
        args: list[str], *, cwd=None, check=True, timeout_seconds=None
    ):  # type: ignore[no-untyped-def]
        _ = cwd, check, timeout_seconds
        assert args[:2] == ["api", "graphql"]
        return type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "thread-1",
                                                "isResolved": False,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "id": "PRRC_kwDO123",
                                                            "databaseId": 444,
                                                            "url": "https://example.invalid/review-comment/444",
                                                            "authorAssociation": "MEMBER",
                                                            "body": "Please cover the webhook path too.",
                                                            "path": "src/example.py",
                                                            "line": 12,
                                                            "updatedAt": "2026-03-30T01:00:00Z",
                                                            "author": {
                                                                "__typename": "User",
                                                                "login": "reviewer",
                                                            },
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    }
                ),
            },
        )()

    monkeypatch.setattr(service, "_gh", _fake_gh)

    threads = service.pr_review_threads(owner="acme", repo="widgets", number=17)

    assert threads == [
        {
            "thread_id": "thread-1",
            "isResolved": False,
            "comments": [
                {
                    "comment_id": "444",
                    "html_url": "https://example.invalid/review-comment/444",
                    "author": {"login": "reviewer"},
                    "author_login": "reviewer",
                    "author_type": "User",
                    "author_association": "MEMBER",
                    "body": "Please cover the webhook path too.",
                    "path": "src/example.py",
                    "line": 12,
                    "updated_at": "2026-03-30T01:00:00Z",
                }
            ],
        }
    ]


def test_create_pull_request_review_comment_reaction_posts_eyes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    gh_calls: list[list[str]] = []

    def _fake_gh(
        args: list[str], *, cwd=None, check=True, timeout_seconds=None
    ):  # type: ignore[no-untyped-def]
        _ = cwd, check, timeout_seconds
        gh_calls.append(args)
        return type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "id": 88,
                        "content": "eyes",
                        "url": "https://api.github.com/reactions/88",
                    }
                ),
            },
        )()

    monkeypatch.setattr(service, "_gh", _fake_gh)

    created = service.create_pull_request_review_comment_reaction(
        owner="acme",
        repo="widgets",
        comment_id=444,
        content="eyes",
    )

    assert created["id"] == 88
    assert gh_calls == [
        [
            "api",
            "--method",
            "POST",
            "repos/acme/widgets/pulls/comments/444/reactions",
            "-H",
            "Accept: application/vnd.github+json",
            "-f",
            "content=eyes",
        ]
    ]
