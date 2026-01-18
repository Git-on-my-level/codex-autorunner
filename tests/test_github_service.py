from typing import Optional

from codex_autorunner.integrations.github import service as github_service
from codex_autorunner.integrations.github.service import (
    GitHubService,
    parse_issue_input,
    parse_pr_input,
)


def test_gh_available_false_when_override_missing(monkeypatch, tmp_path) -> None:
    def fake_resolve(path: str) -> Optional[str]:
        assert path == "/missing/gh"
        return None

    monkeypatch.setattr(github_service, "resolve_executable", fake_resolve)
    svc = GitHubService(tmp_path, raw_config={"github": {"gh_path": "/missing/gh"}})
    assert svc.gh_available() is False


def test_gh_available_true_when_override_resolves(monkeypatch, tmp_path) -> None:
    def fake_resolve(path: str) -> Optional[str]:
        assert path == "/custom/gh"
        return "/custom/gh"

    monkeypatch.setattr(github_service, "resolve_executable", fake_resolve)
    svc = GitHubService(tmp_path, raw_config={"github": {"gh_path": "/custom/gh"}})
    assert svc.gh_available() is True


def test_parse_issue_input_with_hash_prefix() -> None:
    slug, num = parse_issue_input("#123")
    assert slug is None
    assert num == 123


def test_parse_issue_input_without_hash_prefix() -> None:
    slug, num = parse_issue_input("123")
    assert slug is None
    assert num == 123


def test_parse_issue_input_with_url() -> None:
    slug, num = parse_issue_input("https://github.com/org/repo/issues/456")
    assert slug == "org/repo"
    assert num == 456


def test_parse_pr_input_with_hash_prefix() -> None:
    slug, num = parse_pr_input("#123")
    assert slug is None
    assert num == 123


def test_parse_pr_input_without_hash_prefix() -> None:
    slug, num = parse_pr_input("123")
    assert slug is None
    assert num == 123


def test_parse_pr_input_with_url() -> None:
    slug, num = parse_pr_input("https://github.com/org/repo/pull/456")
    assert slug == "org/repo"
    assert num == 456


def test_parse_issue_input_with_hash_prefix_and_spaces() -> None:
    slug, num = parse_issue_input("  #123  ")
    assert slug is None
    assert num == 123


def test_parse_pr_input_with_hash_prefix_and_spaces() -> None:
    slug, num = parse_pr_input("  #123  ")
    assert slug is None
    assert num == 123
