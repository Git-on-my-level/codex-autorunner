from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_autorunner.integrations.telegram.handlers.commands.execution as execution_module
from codex_autorunner.core.injected_context import wrap_injected_context
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
)


class _GitHubServiceStub:
    def __init__(self, _repo_root: Path, raw_config: object = None) -> None:
        self._raw_config = raw_config

    def gh_available(self) -> bool:
        return True

    def gh_authenticated(self) -> bool:
        return True

    def build_context_file_from_url(self, url: str) -> dict[str, str]:
        parsed = execution_module.parse_github_url(url)
        assert parsed is not None
        kind = parsed[1]
        number = parsed[2]
        rel_path = f".codex-autorunner/github_context/{kind}-{number}.md"
        hint = wrap_injected_context(
            f"Context: see {rel_path} (gh available: true; use gh CLI for updates if asked)."
        )
        return {"path": rel_path, "hint": hint, "kind": kind}


def _configure_github_context_test_doubles(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    monkeypatch.setattr(
        execution_module, "_repo_root", lambda _workspace_root: repo_root
    )
    monkeypatch.setattr(execution_module, "load_repo_config", lambda _repo_root: None)
    monkeypatch.setattr(execution_module, "GitHubService", _GitHubServiceStub)


@pytest.mark.anyio
async def test_issue_only_link_injects_branch_and_pr_workflow_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_github_context_test_doubles(monkeypatch, tmp_path)

    handler = ExecutionCommands()
    handler._logger = logging.getLogger("test.telegram.github_context.issue_only")
    record = SimpleNamespace(workspace_path=str(tmp_path))

    prompt = "https://github.com/example/repo/issues/321"
    injected_prompt, injected = await handler._maybe_inject_github_context(
        prompt, record
    )

    assert injected is True
    assert (
        "Context: see .codex-autorunner/github_context/issue-321.md" in injected_prompt
    )
    assert "Issue-only GitHub message detected" in injected_prompt
    assert "latest head branch" in injected_prompt
    assert "Closes #321" in injected_prompt


@pytest.mark.anyio
async def test_issue_link_with_extra_text_does_not_inject_issue_only_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_github_context_test_doubles(monkeypatch, tmp_path)

    handler = ExecutionCommands()
    handler._logger = logging.getLogger("test.telegram.github_context.mixed")
    record = SimpleNamespace(workspace_path=str(tmp_path))

    prompt = "Please fix this issue and add tests: https://github.com/example/repo/issues/321"
    injected_prompt, injected = await handler._maybe_inject_github_context(
        prompt, record
    )

    assert injected is True
    assert (
        "Context: see .codex-autorunner/github_context/issue-321.md" in injected_prompt
    )
    assert "Issue-only GitHub message detected" not in injected_prompt
    assert "latest head branch" not in injected_prompt
