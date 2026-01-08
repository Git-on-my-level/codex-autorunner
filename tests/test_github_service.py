from typing import Optional

from codex_autorunner.integrations.github import service as github_service
from codex_autorunner.integrations.github.service import GitHubService


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
