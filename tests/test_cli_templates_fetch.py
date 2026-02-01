import json
from pathlib import Path
from typing import Optional

import yaml
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.core.git_utils import run_git


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


def _write_templates_config(
    hub_root: Path,
    *,
    enabled: bool,
    repos: list[dict],
) -> None:
    config_path = hub_root / CONFIG_FILENAME
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["templates"] = {"enabled": enabled, "repos": repos}
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def test_templates_fetch_json(hub_env, tmp_path: Path) -> None:
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "# Template\nHello"
    commit, blob_sha = _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

    _write_templates_config(
        hub_env.hub_root,
        enabled=True,
        repos=[
            {
                "id": "local",
                "url": str(repo_path),
                "trusted": True,
                "default_ref": branch,
            }
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "fetch",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)

    assert payload["content"] == content
    assert payload["repo_id"] == "local"
    assert payload["path"] == "tickets/TICKET-REVIEW.md"
    assert payload["ref"] == branch
    assert payload["commit_sha"] == commit
    assert payload["blob_sha"] == blob_sha
    assert payload["trusted"] is True
    assert payload["scan_decision"] is None


def test_templates_fetch_disabled(hub_env) -> None:
    _write_templates_config(hub_env.hub_root, enabled=False, repos=[])

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "fetch",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
        ],
    )

    assert result.exit_code == 1
    assert "Templates are disabled." in result.output


def test_templates_fetch_network_unavailable_includes_details(
    hub_env, tmp_path: Path
) -> None:
    """Test that NetworkUnavailableError includes repo_id, ref, path, and git error details."""
    # Configure a template repo that doesn't exist
    _write_templates_config(
        hub_env.hub_root,
        enabled=True,
        repos=[
            {
                "id": "nonexistent",
                "url": "https://example.com/nonexistent-repo.git",
                "trusted": True,
                "default_ref": "main",
            }
        ],
    )

    runner = CliRunner()

    # Try to fetch from the non-existent repo - should fail with detailed error
    result = runner.invoke(
        app,
        [
            "templates",
            "fetch",
            "nonexistent:tickets/TICKET-TEST.md",
            "--repo",
            str(hub_env.repo_root),
        ],
    )

    assert result.exit_code == 1
    assert "repo_id=nonexistent" in result.output
    assert "ref=main" in result.output
    assert "path=tickets/TICKET-TEST.md" in result.output
    assert "Hint: Fetch once while online to seed the cache" in result.output
