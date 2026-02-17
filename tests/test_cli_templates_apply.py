from pathlib import Path
from typing import Optional

import yaml
from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.config import CONFIG_FILENAME
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter


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


def test_templates_apply_next_index_writes_file(hub_env, tmp_path: Path) -> None:
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "---\nagent: codex\ndone: false\n---\n\n# Template\nHello\n"
    _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

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

    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text("---\nagent: codex\ndone: false\n---\n")
    (ticket_dir / "TICKET-003.md").write_text("---\nagent: codex\ndone: false\n---\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
        ],
    )

    assert result.exit_code == 0
    created_path = ticket_dir / "TICKET-002.md"
    assert created_path.exists()
    frontmatter, body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False
    assert frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{branch}"
    assert frontmatter["template_commit"]
    assert frontmatter["template_blob"]
    assert "Hello" in body


def test_templates_apply_set_agent_overrides_frontmatter(
    hub_env, tmp_path: Path
) -> None:
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "---\nagent: opencode\ndone: false\n---\n\n# Template\nHello\n"
    _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

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
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--set-agent",
            "user",
        ],
    )

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()
    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "user"


def test_templates_apply_with_provenance_includes_metadata(
    hub_env, tmp_path: Path
) -> None:
    """Test that --provenance adds provenance keys to ticket frontmatter."""
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "---\nagent: codex\ndone: false\n---\n\n# Template\nHello\n"
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
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--provenance",
        ],
    )

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()

    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )

    # Check provenance keys
    assert frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{branch}"
    assert frontmatter["template_commit"] == commit
    assert frontmatter["template_blob"] == blob_sha
    assert frontmatter["template_trusted"] is True
    assert frontmatter["template_scan"] == "skipped"

    # Original frontmatter keys should be preserved
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False


def test_templates_apply_without_provenance_no_metadata(
    hub_env, tmp_path: Path
) -> None:
    """Test that --no-provenance (default) does not add provenance keys."""
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "---\nagent: codex\ndone: false\n---\n\n# Template\nHello\n"
    _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

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
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--no-provenance",
        ],
    )

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()

    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )

    # Provenance keys should not be present
    assert "template" not in frontmatter
    assert "template_commit" not in frontmatter
    assert "template_blob" not in frontmatter
    assert "template_trusted" not in frontmatter
    assert "template_scan" not in frontmatter

    # Original frontmatter keys should be preserved
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False


def test_template_apply_alias_with_out_writes_to_requested_directory(
    hub_env, tmp_path: Path
) -> None:
    repo_path = tmp_path / "templates_repo"
    branch = _init_repo(repo_path)
    content = "---\nagent: codex\ndone: false\n---\n\n# Template\nHello\n"
    _commit_file(repo_path, "tickets/TICKET-REVIEW.md", content)

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

    out_dir = hub_env.repo_root / ".codex-autorunner" / "tickets-out"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "template",
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0
    created_path = out_dir / "TICKET-001.md"
    assert created_path.exists()
    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False
    assert frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{branch}"
