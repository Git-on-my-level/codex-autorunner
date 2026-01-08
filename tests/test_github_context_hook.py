import json
from pathlib import Path

import pytest

from codex_autorunner.github import GitHubService, parse_github_url


def _fixture(path: str) -> dict:
    data = Path(path).read_text(encoding="utf-8")
    return json.loads(data)


def _stub_repo_info(slug: str):
    return type(
        "Repo", (), {"name_with_owner": slug, "url": f"https://github.com/{slug}"}
    )()


def test_github_context_issue_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    issue = _fixture("tests/fixtures/github_issue.json")
    url = issue.get("url")
    assert url
    parsed = parse_github_url(url)
    assert parsed
    slug, kind, number = parsed
    assert kind == "issue"
    svc = GitHubService(tmp_path)
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(svc, "repo_info", lambda: _stub_repo_info(slug))
    monkeypatch.setattr(svc, "issue_view", lambda **_: issue)

    result = svc.build_context_file_from_url(url)
    assert result and result.get("path")
    path = tmp_path / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "GitHub Issue Context" in text
    assert f"Repo: {slug}" in text
    assert f"Issue: #{number}" in text
    comment_count = len(issue.get("comments") or [])
    assert f"Comments: {comment_count}" in text


def test_github_context_issue_comments_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    issue = _fixture("tests/fixtures/github_issue.json")
    url = issue.get("url")
    assert url
    parsed = parse_github_url(url)
    assert parsed
    slug, kind, _number = parsed
    assert kind == "issue"
    issue = dict(issue)
    issue["comments"] = {"totalCount": 2, "nodes": [{"id": "1"}, {"id": "2"}]}
    svc = GitHubService(tmp_path)
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(svc, "repo_info", lambda: _stub_repo_info(slug))
    monkeypatch.setattr(svc, "issue_view", lambda **_: issue)

    result = svc.build_context_file_from_url(url)
    assert result and result.get("path")
    path = tmp_path / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "Comments: 2" in text


def test_github_context_pr_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pr = _fixture("tests/fixtures/github_pr.json")
    url = pr.get("url")
    assert url
    parsed = parse_github_url(url)
    assert parsed
    slug, kind, number = parsed
    assert kind == "pr"
    svc = GitHubService(tmp_path)
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(svc, "repo_info", lambda: _stub_repo_info(slug))
    monkeypatch.setattr(svc, "pr_view", lambda **_: pr)
    monkeypatch.setattr(svc, "pr_review_threads", lambda **_: [])

    result = svc.build_context_file_from_url(url)
    assert result and result.get("path")
    path = tmp_path / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "GitHub PR Context" in text
    assert f"Repo: {slug}" in text
    assert f"PR: #{number}" in text
    assert "Files:" in text


def test_github_context_pr_review_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = _fixture("tests/fixtures/github_pr.json")
    url = pr.get("url")
    assert url
    parsed = parse_github_url(url)
    assert parsed
    slug, kind, number = parsed
    assert kind == "pr"
    svc = GitHubService(tmp_path)
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(svc, "repo_info", lambda: _stub_repo_info(slug))
    monkeypatch.setattr(svc, "pr_view", lambda **_: pr)
    monkeypatch.setattr(
        svc,
        "pr_review_threads",
        lambda **_: [
            {
                "isResolved": False,
                "comments": [
                    {
                        "author": {"login": "octocat"},
                        "body": "Please rename this variable.",
                        "path": "src/app.py",
                        "line": 42,
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ],
    )

    result = svc.build_context_file_from_url(url)
    assert result and result.get("path")
    path = tmp_path / result["path"]
    text = path.read_text(encoding="utf-8")
    assert "Review Threads:" in text
    assert "src/app.py:42 octocat" in text
    assert "Please rename this variable." in text


def test_github_context_rejects_other_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pr = _fixture("tests/fixtures/github_pr.json")
    url = pr.get("url")
    assert url
    parsed = parse_github_url(url)
    assert parsed
    slug, _kind, _number = parsed
    svc = GitHubService(tmp_path)
    monkeypatch.setattr(svc, "gh_available", lambda: True)
    monkeypatch.setattr(svc, "gh_authenticated", lambda: True)
    monkeypatch.setattr(svc, "repo_info", lambda: _stub_repo_info(f"{slug}-other"))
    monkeypatch.setattr(svc, "pr_view", lambda **_: pr)

    result = svc.build_context_file_from_url(url)
    assert result is None
