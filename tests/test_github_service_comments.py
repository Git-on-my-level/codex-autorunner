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
