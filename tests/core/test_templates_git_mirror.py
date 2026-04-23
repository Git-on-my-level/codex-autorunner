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


_SHARED_FILES = {
    "TICKET-REVIEW.md": "# Template\nHello",
    "TICKET-ONE.md": "# Template One\nOne",
    "TICKET-TWO.md": "# Template Two\nTwo",
    "TICKET-OK.md": "ok",
}


@pytest.fixture(scope="module")
def _shared_mirror_repo(tmp_path_factory):
    base = tmp_path_factory.mktemp("shared_mirror")
    repo_path = base / "repo"
    hub_root = base / "hub"
    hub_root.mkdir()
    branch = _init_repo(repo_path)
    for name, content in _SHARED_FILES.items():
        fp = repo_path / "tickets" / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    run_git(["add", "tickets/"], repo_path, check=True)
    run_git(["commit", "-m", "add templates"], repo_path, check=True)
    commit = (run_git(["rev-parse", "HEAD"], repo_path).stdout or "").strip()
    files = {}
    for name, content in _SHARED_FILES.items():
        rel = f"tickets/{name}"
        tree_entry = (
            run_git(["ls-tree", commit, "--", rel], repo_path).stdout or ""
        ).strip()
        files[name] = {
            "blob_sha": tree_entry.split()[2],
            "content": content,
        }
    return {
        "repo_path": repo_path,
        "branch": branch,
        "commit": commit,
        "files": files,
        "hub_root": hub_root,
    }


def _mirror_config(shared, repo_id: str = "local") -> TemplateRepoConfig:
    return TemplateRepoConfig(
        id=repo_id,
        url=str(shared["repo_path"]),
        trusted=True,
        default_ref=shared["branch"],
    )


def test_fetch_template_from_local_repo(_shared_mirror_repo) -> None:
    shared = _shared_mirror_repo
    review = shared["files"]["TICKET-REVIEW.md"]

    fetched = fetch_template(
        repo=_mirror_config(shared),
        hub_root=shared["hub_root"],
        template_ref="local:tickets/TICKET-REVIEW.md",
    )

    assert fetched.commit_sha == shared["commit"]
    assert fetched.blob_sha == review["blob_sha"]
    assert fetched.content == review["content"]
    assert fetched.ref == shared["branch"]


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


def test_fetch_template_missing_path(_shared_mirror_repo) -> None:
    shared = _shared_mirror_repo

    with pytest.raises(TemplateNotFoundError):
        fetch_template(
            repo=_mirror_config(shared),
            hub_root=shared["hub_root"],
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


def test_fetch_template_content_change_changes_blob_sha(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    branch = _init_repo(repo_path)
    initial_content = "# Initial Template\nHello"
    initial_commit, initial_blob_sha = _commit_file(
        repo_path, "tickets/TICKET-CHANGE.md", initial_content
    )

    repo = TemplateRepoConfig(
        id="local",
        url=str(repo_path),
        trusted=True,
        default_ref=branch,
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    fetched_initial = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-CHANGE.md",
    )

    assert fetched_initial.commit_sha == initial_commit
    assert fetched_initial.blob_sha == initial_blob_sha
    assert fetched_initial.content == initial_content

    modified_content = "# Modified Template\nChanged"
    modified_commit, modified_blob_sha = _commit_file(
        repo_path, "tickets/TICKET-CHANGE.md", modified_content
    )

    fetched_modified = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-CHANGE.md",
    )

    assert fetched_modified.commit_sha == modified_commit
    assert fetched_modified.blob_sha == modified_blob_sha
    assert fetched_modified.content == modified_content

    assert initial_commit != modified_commit
    assert initial_blob_sha != modified_blob_sha


def test_fetch_template_different_file_different_blob_sha(
    _shared_mirror_repo,
) -> None:
    shared = _shared_mirror_repo
    repo = _mirror_config(shared)

    fetched_one = fetch_template(
        repo=repo,
        hub_root=shared["hub_root"],
        template_ref="local:tickets/TICKET-ONE.md",
    )
    fetched_two = fetch_template(
        repo=repo,
        hub_root=shared["hub_root"],
        template_ref="local:tickets/TICKET-TWO.md",
    )

    assert fetched_one.blob_sha == shared["files"]["TICKET-ONE.md"]["blob_sha"]
    assert fetched_two.blob_sha == shared["files"]["TICKET-TWO.md"]["blob_sha"]
    assert fetched_one.blob_sha != fetched_two.blob_sha


def test_fetch_template_with_explicit_ref(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path, branch="main")
    content = "# Branch Template\nBranch content"
    commit, blob_sha = _commit_file(repo_path, "tickets/TICKET-BRANCH.md", content)

    repo = TemplateRepoConfig(
        id="local",
        url=str(repo_path),
        trusted=True,
        default_ref="other",
    )
    hub_root = tmp_path / "hub"
    hub_root.mkdir()

    fetched = fetch_template(
        repo=repo,
        hub_root=hub_root,
        template_ref="local:tickets/TICKET-BRANCH.md@main",
    )

    assert fetched.commit_sha == commit
    assert fetched.blob_sha == blob_sha
    assert fetched.content == content
    assert fetched.ref == "main"
