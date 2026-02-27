from __future__ import annotations

import logging
from pathlib import Path

import pytest

import codex_autorunner.integrations.github.context_injection as context_module
from codex_autorunner.core.injected_context import wrap_injected_context
from codex_autorunner.core.utils import RepoNotFoundError


class _GitHubServiceStub:
    def __init__(self, repo_root: Path, raw_config: object = None) -> None:
        self.repo_root = repo_root
        self.raw_config = raw_config
        self.calls: list[tuple[str, bool]] = []

    def gh_available(self) -> bool:
        return True

    def gh_authenticated(self) -> bool:
        return True

    def build_context_file_from_url(
        self, url: str, *, allow_cross_repo: bool = False
    ) -> dict[str, str]:
        self.calls.append((url, allow_cross_repo))
        return {
            "path": ".codex-autorunner/github_context/issue-123.md",
            "hint": wrap_injected_context("Context: injected"),
        }


@pytest.mark.anyio
async def test_pma_injection_uses_user_message_links_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_module, "find_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(context_module, "load_repo_config", lambda _path: None)
    monkeypatch.setattr(context_module, "GitHubService", _GitHubServiceStub)

    prompt_text = "PMA docs include https://github.com/example/repo/issues/999"
    injected_prompt, injected = await context_module.maybe_inject_github_context(
        prompt_text=prompt_text,
        link_source_text="hello there",
        workspace_root=tmp_path,
        logger=logging.getLogger("test.github_context.user_message_only"),
        event_prefix="test.github_context",
        allow_cross_repo=True,
    )

    assert injected is False
    assert injected_prompt == prompt_text


@pytest.mark.anyio
async def test_pma_injection_falls_back_to_workspace_when_repo_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context_module,
        "find_repo_root",
        lambda _path: (_ for _ in ()).throw(RepoNotFoundError("no repo")),
    )
    monkeypatch.setattr(context_module, "load_repo_config", lambda _path: None)
    monkeypatch.setattr(context_module, "GitHubService", _GitHubServiceStub)

    prompt_text = "PMA prompt"
    source_text = "https://github.com/example/repo/issues/123"
    injected_prompt, injected = await context_module.maybe_inject_github_context(
        prompt_text=prompt_text,
        link_source_text=source_text,
        workspace_root=tmp_path,
        logger=logging.getLogger("test.github_context.repo_fallback"),
        event_prefix="test.github_context",
        allow_cross_repo=True,
    )

    assert injected is True
    assert "Context: injected" in injected_prompt
    assert "Issue-only GitHub message detected" in injected_prompt
    assert "Closes #123" in injected_prompt


@pytest.mark.anyio
async def test_pma_injection_skips_when_repo_not_found_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        context_module,
        "find_repo_root",
        lambda _path: (_ for _ in ()).throw(RepoNotFoundError("no repo")),
    )
    monkeypatch.setattr(context_module, "load_repo_config", lambda _path: None)
    monkeypatch.setattr(context_module, "GitHubService", _GitHubServiceStub)

    prompt_text = "PMA prompt"
    source_text = "https://github.com/example/repo/issues/123"
    injected_prompt, injected = await context_module.maybe_inject_github_context(
        prompt_text=prompt_text,
        link_source_text=source_text,
        workspace_root=tmp_path,
        logger=logging.getLogger("test.github_context.no_fallback"),
        event_prefix="test.github_context",
        allow_cross_repo=False,
    )

    assert injected is False
    assert injected_prompt == prompt_text
