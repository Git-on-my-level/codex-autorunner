from pathlib import Path
from typing import Optional

import pytest
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


_APPLY_TEMPLATE_CONTENT = "---\nagent: codex\ndone: false\n---\n\n# Template\nHello\n"
_APPLY_TEMPLATE_OVERRIDE_CONTENT = (
    "---\nagent: opencode\ndone: false\n---\n\n# Template\nHello\n"
)


@pytest.fixture(scope="module")
def _shared_apply_repo(tmp_path_factory):
    base = tmp_path_factory.mktemp("shared_apply")
    repo_path = base / "templates_repo"
    branch = _init_repo(repo_path)
    commit, blob_sha = _commit_file(
        repo_path, "tickets/TICKET-REVIEW.md", _APPLY_TEMPLATE_CONTENT
    )
    return {
        "repo_path": repo_path,
        "branch": branch,
        "commit": commit,
        "blob_sha": blob_sha,
    }


def _apply_config(shared, hub_root, template_content=None):
    if template_content is None:
        template_content = _APPLY_TEMPLATE_CONTENT
    _write_templates_config(
        hub_root,
        enabled=True,
        repos=[
            {
                "id": "local",
                "url": str(shared["repo_path"]),
                "trusted": True,
                "default_ref": shared["branch"],
            }
        ],
    )


def _invoke_apply(hub_env, extra_args=None):
    runner = CliRunner()
    args = [
        "templates",
        "apply",
        "local:tickets/TICKET-REVIEW.md",
        "--repo",
        str(hub_env.repo_root),
    ]
    if extra_args:
        args.extend(extra_args)
    return runner.invoke(app, args)


def test_templates_apply_next_index_writes_file(
    _shared_apply_repo, hub_env, tmp_path: Path
) -> None:
    shared = _shared_apply_repo
    _apply_config(shared, hub_env.hub_root)

    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text("---\nagent: codex\ndone: false\n---\n")
    (ticket_dir / "TICKET-003.md").write_text("---\nagent: codex\ndone: false\n---\n")

    result = _invoke_apply(hub_env)

    assert result.exit_code == 0
    created_path = ticket_dir / "TICKET-002.md"
    assert created_path.exists()
    frontmatter, body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False
    assert (
        frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{shared['branch']}"
    )
    assert frontmatter["template_commit"]
    assert frontmatter["template_blob"]
    assert "Hello" in body


def test_templates_apply_set_agent_overrides_frontmatter(
    _shared_apply_repo, hub_env, tmp_path: Path
) -> None:
    shared = _shared_apply_repo
    _apply_config(shared, hub_env.hub_root)

    result = _invoke_apply(hub_env, ["--set-agent", "user"])

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()
    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "user"


def test_templates_apply_with_provenance_includes_metadata(
    _shared_apply_repo, hub_env, tmp_path: Path
) -> None:
    shared = _shared_apply_repo
    _apply_config(shared, hub_env.hub_root)

    result = _invoke_apply(hub_env, ["--provenance"])

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()

    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )

    assert (
        frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{shared['branch']}"
    )
    assert frontmatter["template_commit"] == shared["commit"]
    assert frontmatter["template_blob"] == shared["blob_sha"]
    assert frontmatter["template_trusted"] is True
    assert frontmatter["template_scan"] == "skipped"
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False


def test_templates_apply_without_provenance_no_metadata(
    _shared_apply_repo, hub_env, tmp_path: Path
) -> None:
    shared = _shared_apply_repo
    _apply_config(shared, hub_env.hub_root)

    result = _invoke_apply(hub_env, ["--no-provenance"])

    assert result.exit_code == 0
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    created_path = ticket_dir / "TICKET-001.md"
    assert created_path.exists()

    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )

    assert "template" not in frontmatter
    assert "template_commit" not in frontmatter
    assert "template_blob" not in frontmatter
    assert "template_trusted" not in frontmatter
    assert "template_scan" not in frontmatter
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False


def test_template_apply_rejects_removed_out_alias(
    _shared_apply_repo, hub_env, tmp_path: Path
) -> None:
    shared = _shared_apply_repo
    _apply_config(shared, hub_env.hub_root)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "templates",
            "apply",
            "local:tickets/TICKET-REVIEW.md",
            "--repo",
            str(hub_env.repo_root),
            "--out",
            str(hub_env.repo_root / ".codex-autorunner" / "tickets-out"),
        ],
    )

    assert result.exit_code != 0

    created_path = hub_env.repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md"
    assert not created_path.exists()
    result = _invoke_apply(hub_env)

    assert result.exit_code == 0
    assert created_path.exists()
    frontmatter, _body = parse_markdown_frontmatter(
        created_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False
    assert (
        frontmatter["template"] == f"local:tickets/TICKET-REVIEW.md@{shared['branch']}"
    )
