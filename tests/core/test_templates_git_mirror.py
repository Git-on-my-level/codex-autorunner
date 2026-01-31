import shutil
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.core.config import TemplateRepoConfig
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.templates.git_mirror import (
    NetworkUnavailableError,
    TemplateNotFoundError,
    fetch_template,
)


def _init_repo(repo_path: Path, *, branch: Optional[str] = None) -> str:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    if branch:
        run_git(["checkout", "-b", branch], repo_path, check=True)
    return (
        run_git(["symbolic-ref", "--short", "HEAD"], repo_path, check=True).stdout or ""
    ).strip()


def _commit_file(repo_path: Path, rel_path: str, content: str) -> tuple[str, str]:
    file_path = repo_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    run_git(["add", rel_path], repo_path, check=True)
    run_git(["commit", "-m", "add template"], repo_path, check=True)
    commit = (run_git(["rev-parse", "HEAD"], repo_path).stdout or "").strip()
    tree_entry = (
        run_git(["ls-tree", commit, "--", rel_path], repo_path).stdout or ""
    ).strip()
    blob_sha = tree_entry.split()[2]
    return commit, blob_sha


def test_fetch_template_from_local_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    branch = _init_repo(repo_path)
    content = "# Template\nHello"
    commit, blob_sha = _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

    repo = TemplateRepoConfig(
        id="local",
        url=str(repo_path),
        trusted=True,
        default_ref=branch,
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    fetched = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-REVIEW.md",
    )

    assert fetched.commit_sha == commit
    assert fetched.blob_sha == blob_sha
    assert fetched.content == content
    assert fetched.ref == branch


def test_fetch_template_offline_fallback(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    branch = _init_repo(repo_path)
    content = "Offline content"
    _commit_file(repo_path, "tickets/TICKET-OFFLINE.md", content)

    repo = TemplateRepoConfig(
        id="local",
        url=str(repo_path),
        trusted=True,
        default_ref=branch,
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    fetched = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-OFFLINE.md",
    )
    assert fetched.content == content

    shutil.rmtree(repo_path)

    fetched_again = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-OFFLINE.md",
    )
    assert fetched_again.content == content


def test_fetch_template_missing_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    branch = _init_repo(repo_path)
    _commit_file(repo_path, "tickets/TICKET-OK.md", "ok")

    repo = TemplateRepoConfig(
        id="local",
        url=str(repo_path),
        trusted=True,
        default_ref=branch,
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    with pytest.raises(TemplateNotFoundError):
        fetch_template(
            repo=repo,
            hub_root=hub_root,
            template_ref="local:tickets/MISSING.md",
        )


def test_fetch_template_network_unavailable(tmp_path: Path) -> None:
    repo = TemplateRepoConfig(
        id="local",
        url=str(tmp_path / "missing-repo"),
        trusted=True,
        default_ref="main",
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    with pytest.raises(NetworkUnavailableError):
        fetch_template(
            repo=repo,
            hub_root=hub_root,
            template_ref="local:tickets/MISSING.md",
        )
