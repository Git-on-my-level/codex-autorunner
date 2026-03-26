from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.integrations.github.service import GitHubService, RepoInfo


def test_discover_pr_binding_summary_for_branch_returns_normalized_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    seen: dict[str, object] = {}

    def _fake_pr_for_branch(
        *, branch: str, cwd: Path | None = None
    ) -> dict[str, object]:
        seen["branch"] = branch
        seen["cwd"] = cwd
        return {
            "number": "17",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "feature/login",
            "baseRefName": "main",
            "url": "https://github.com/acme/widgets/pull/17",
            "title": "Login flow",
        }

    monkeypatch.setattr(service, "pr_for_branch", _fake_pr_for_branch)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )

    summary = service.discover_pr_binding_summary(
        branch="feature/login", cwd=tmp_path / "repo"
    )

    assert summary == {
        "repo_slug": "acme/widgets",
        "pr_number": 17,
        "pr_state": "open",
        "head_branch": "feature/login",
        "base_branch": "main",
    }
    assert seen == {"branch": "feature/login", "cwd": tmp_path / "repo"}


def test_discover_pr_binding_summary_uses_current_branch_when_unspecified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    monkeypatch.setattr(service, "current_branch", lambda *, cwd=None: "feature/draft")
    monkeypatch.setattr(
        service,
        "pr_for_branch",
        lambda *, branch, cwd=None: {
            "number": 23,
            "state": "OPEN",
            "isDraft": True,
            "headRefName": branch,
        },
    )
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )

    summary = service.discover_pr_binding_summary()

    assert summary == {
        "repo_slug": "acme/widgets",
        "pr_number": 23,
        "pr_state": "draft",
        "head_branch": "feature/draft",
    }


def test_discover_pr_binding_summary_returns_none_when_branch_has_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    monkeypatch.setattr(
        service, "current_branch", lambda *, cwd=None: "feature/missing"
    )
    monkeypatch.setattr(service, "pr_for_branch", lambda *, branch, cwd=None: None)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: pytest.fail("repo_info should not be called when no PR is found"),
    )

    assert service.discover_pr_binding_summary() is None


def test_sync_pr_does_not_append_duplicate_close_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = GitHubService(tmp_path, raw_config={})
    edit_calls: list[list[str]] = []

    monkeypatch.setattr(service, "gh_authenticated", lambda: True)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )
    monkeypatch.setattr(
        service,
        "read_link_state",
        lambda: {"issue": {"number": 123}},
    )
    monkeypatch.setattr(service, "current_branch", lambda *, cwd=None: "feature/login")
    monkeypatch.setattr(service, "is_clean", lambda *, cwd=None: True)
    monkeypatch.setattr(
        service,
        "pr_for_branch",
        lambda *, branch, cwd=None: {
            "url": "https://github.com/acme/widgets/pull/17",
            "number": 17,
            "state": "OPEN",
            "isDraft": False,
            "headRefName": branch,
            "baseRefName": "main",
            "title": "Login flow",
        },
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.github.service._run_codex_sync_agent",
        lambda **_: None,
    )

    def _fake_gh(
        args: list[str], *, cwd=None, check=True, timeout_seconds=None
    ):  # type: ignore[no-untyped-def]
        if args[:3] == ["pr", "view", "https://github.com/acme/widgets/pull/17"]:
            return type(
                "Proc", (), {"stdout": '{"body":"Fixes #123\\n\\nAlready done"}'}
            )()
        if args[:3] == ["pr", "edit", "https://github.com/acme/widgets/pull/17"]:
            edit_calls.append(list(args))
            return type("Proc", (), {"stdout": ""})()
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(service, "_gh", _fake_gh)
    monkeypatch.setattr(service, "write_link_state", lambda state: None)

    result = service.sync_pr(draft=False, title="Login flow", body="Initial body")

    assert result["links"]["url"] == "https://github.com/acme/widgets/pull/17"
    assert edit_calls == []


def _write_manifest(hub_root: Path, *, repo_rel: str, repo_id: str = "repo-1") -> None:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                f"  - id: {repo_id}",
                f"    path: {repo_rel}",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_discover_pr_binding_summary_prefers_canonical_binding_for_hub_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")
    PrBindingStore(hub_root).upsert_binding(
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=88,
        pr_state="open",
        head_branch="feature/login",
        base_branch="main",
    )

    service = GitHubService(repo_root, raw_config={})
    monkeypatch.setattr(
        service,
        "pr_for_branch",
        lambda **_: pytest.fail(
            "pr_for_branch should not run when canonical binding exists"
        ),
    )
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: pytest.fail("repo_info should not run when canonical binding exists"),
    )

    assert service.discover_pr_binding_summary(branch="feature/login") == {
        "repo_slug": "acme/widgets",
        "pr_number": 88,
        "pr_state": "open",
        "head_branch": "feature/login",
        "base_branch": "main",
    }


def test_sync_pr_persists_binding_and_keeps_link_state_as_session_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_root = tmp_path / "hub"
    repo_root = hub_root / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    _write_manifest(hub_root, repo_rel="workspace/repo")

    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        repo_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/login"},
    )

    service = GitHubService(repo_root, raw_config={})
    monkeypatch.setattr(service, "gh_authenticated", lambda: True)
    monkeypatch.setattr(
        service,
        "repo_info",
        lambda: RepoInfo(
            name_with_owner="acme/widgets",
            url="https://github.com/acme/widgets",
            default_branch="main",
        ),
    )
    monkeypatch.setattr(service, "read_link_state", lambda: {"issue": {"number": 123}})
    monkeypatch.setattr(service, "current_branch", lambda *, cwd=None: "feature/login")
    monkeypatch.setattr(service, "is_clean", lambda *, cwd=None: True)
    monkeypatch.setattr(
        "codex_autorunner.integrations.github.service._run_codex_sync_agent",
        lambda **_: None,
    )
    monkeypatch.setattr(
        service,
        "pr_for_branch",
        lambda *, branch, cwd=None: {
            "url": "https://github.com/acme/widgets/pull/17",
            "number": 17,
            "state": "OPEN",
            "isDraft": False,
            "headRefName": branch,
            "baseRefName": "main",
            "title": "Login flow",
        },
    )

    def _fake_gh(
        args: list[str], *, cwd=None, check=True, timeout_seconds=None
    ):  # type: ignore[no-untyped-def]
        if args[:3] == ["pr", "view", "https://github.com/acme/widgets/pull/17"]:
            return type("Proc", (), {"stdout": json.dumps({"body": "Fixes #123"})})()
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(service, "_gh", _fake_gh)

    written: dict[str, object] = {}

    def _capture_link_state(state: dict[str, object]) -> dict[str, object]:
        written.update(state)
        return state

    monkeypatch.setattr(service, "write_link_state", _capture_link_state)

    result = service.sync_pr(draft=False, title="Login flow", body="Initial body")

    binding = PrBindingStore(hub_root).get_binding_by_pr(
        provider="github",
        repo_slug="acme/widgets",
        pr_number=17,
    )
    assert binding is not None
    assert binding.repo_id == "repo-1"
    assert binding.head_branch == "feature/login"
    assert binding.base_branch == "main"
    assert binding.pr_state == "open"
    assert binding.thread_target_id == thread["managed_thread_id"]

    assert written["issue"] == {"number": 123}
    assert written["repo"] == {
        "nameWithOwner": "acme/widgets",
        "url": "https://github.com/acme/widgets",
    }
    assert written["pr"] == {
        "number": 17,
        "url": "https://github.com/acme/widgets/pull/17",
        "title": "Login flow",
    }
    assert "baseBranch" not in written
    assert "headBranch" not in written
    assert isinstance(written["updatedAtMs"], int)
    assert result["links"]["url"] == "https://github.com/acme/widgets/pull/17"
