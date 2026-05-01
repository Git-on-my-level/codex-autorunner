from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from codex_autorunner.core.apps import (
    AppApplyError,
    apply_app_entrypoint,
    get_installed_app,
    install_app,
)
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.ticket_linter_cli import LINTER_REL_PATH
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> str:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)
    return (run_git(["rev-parse", "HEAD"], repo_path, check=True).stdout or "").strip()


def _configure_apps_repo(
    hub_root: Path, app_repo: Path, *, trusted: bool = True
) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "local",
                "url": str(app_repo),
                "trusted": trusted,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _write_valid_app(app_repo: Path) -> None:
    app_root = app_repo / "apps" / "hello"
    (app_root / "templates").mkdir(parents=True, exist_ok=True)
    (app_root / "car-app.yaml").write_text(
        """schema_version: 1
id: local.hello
name: Hello App
version: 1.0.0
description: Apply test app.
entrypoint:
  template: templates/bootstrap.md
inputs:
  goal:
    required: true
    description: Primary goal.
  count:
    required: false
    description: Example typed input.
tools:
  check:
    argv: ["python3", "scripts/check.py"]
templates:
  followup:
    path: templates/followup.md
    description: Follow-up ticket template.
""",
        encoding="utf-8",
    )
    (app_root / "templates" / "bootstrap.md").write_text(
        """---
agent: codex
done: false
title: Hello Ticket
---

# Bootstrap
Base body.
""",
        encoding="utf-8",
    )
    (app_root / "templates" / "followup.md").write_text(
        """---
agent: opencode
done: false
title: Follow-up Ticket
---

# Follow-up
Secondary body.
""",
        encoding="utf-8",
    )
    (app_root / "scripts").mkdir(exist_ok=True)
    (app_root / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")


def _run_ticket_linter(repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo_root / LINTER_REL_PATH)],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )


def _setup_apply_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files

    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_root, git_required=False)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    _write_valid_app(app_repo)
    _commit_repo(app_repo, "add hello app")
    return hub_root, repo_root, app_repo


def _setup_untrusted_apply_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files

    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    seed_repo_files(repo_root, git_required=False)
    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo, trusted=False)
    _write_valid_app(app_repo)
    _commit_repo(app_repo, "add untrusted app")
    return hub_root, repo_root, app_repo


def test_apply_installed_app_by_id_creates_ticket_and_persists_inputs(
    tmp_path: Path,
) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    result = apply_app_entrypoint(
        repo_root,
        "local.hello",
        app_inputs={"goal": "demo", "count": 3},
    )

    assert result.ticket_index == 1
    assert result.install_changed is False
    assert result.ticket_path.name == "TICKET-001.md"
    assert result.ticket_path.exists()
    assert result.apply_inputs_path.exists()

    frontmatter, body = parse_markdown_frontmatter(
        result.ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "codex"
    assert frontmatter["done"] is False
    assert frontmatter["app"] == "local.hello"
    assert "## App Inputs" in body
    assert "- `goal`: `demo`" in body
    assert "- `count`: `3`" in body

    persisted = json.loads(result.apply_inputs_path.read_text(encoding="utf-8"))
    assert persisted["app_id"] == "local.hello"
    assert persisted["ticket_index"] == 1
    assert persisted["inputs"] == {"goal": "demo", "count": 3}


def test_apply_source_ref_installs_implicitly(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)

    result = apply_app_entrypoint(
        repo_root,
        "local:apps/hello",
        hub_config=hub_config,
        hub_root=hub_root,
        app_inputs={"goal": "implicit"},
    )

    installed = get_installed_app(repo_root, "local.hello")
    assert result.install_changed is True
    assert installed is not None
    assert result.ticket_path.exists()
    assert result.source_ref == "local:apps/hello@main"


def test_apply_refuses_untrusted_app_template(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_untrusted_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_result = install_app(hub_config, hub_root, repo_root, "local:apps/hello")
    assert install_result.app.lock.trusted is False

    with pytest.raises(AppApplyError, match="is untrusted"):
        apply_app_entrypoint(
            repo_root,
            "local.hello",
            app_inputs={"goal": "blocked"},
        )


def test_apply_refuses_untrusted_source_ref_before_install(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_untrusted_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)

    with pytest.raises(AppApplyError, match="repo local is untrusted"):
        apply_app_entrypoint(
            repo_root,
            "local:apps/hello",
            hub_config=hub_config,
            hub_root=hub_root,
            app_inputs={"goal": "blocked"},
        )

    assert get_installed_app(repo_root, "local.hello") is None


def test_apply_injects_provenance_frontmatter(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_result = install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    result = apply_app_entrypoint(
        repo_root,
        "local.hello",
        app_inputs={"goal": "prov"},
    )

    frontmatter, _body = parse_markdown_frontmatter(
        result.ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["app"] == "local.hello"
    assert frontmatter["app_version"] == install_result.app.app_version
    assert frontmatter["app_source"] == "local:apps/hello@main"
    assert frontmatter["app_commit"] == install_result.app.lock.commit_sha
    assert frontmatter["app_manifest_sha"] == install_result.app.lock.manifest_sha
    assert frontmatter["app_bundle_sha"] == install_result.app.lock.bundle_sha


def test_apply_refuses_duplicate_ticket_index(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text(
        '---\nticket_id: "tkt_existing001"\nagent: codex\ndone: false\n---\n',
        encoding="utf-8",
    )

    with pytest.raises(AppApplyError, match="already exists"):
        apply_app_entrypoint(
            repo_root,
            "local.hello",
            at=1,
            app_inputs={"goal": "dup"},
        )


def test_apply_generated_ticket_passes_lint_without_dependency_frontmatter(
    tmp_path: Path,
) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    result = apply_app_entrypoint(
        repo_root,
        "local.hello",
        suffix="app",
        app_inputs={"goal": "lint", "count": 2},
    )

    frontmatter, _body = parse_markdown_frontmatter(
        result.ticket_path.read_text(encoding="utf-8")
    )
    lint_result = _run_ticket_linter(repo_root)

    assert result.ticket_path.name == "TICKET-001-app.md"
    assert "depends_on" not in frontmatter
    assert "blocked_by" not in frontmatter
    assert lint_result.returncode == 0
    assert lint_result.stdout == "OK: 1 ticket(s) linted.\n"
    assert lint_result.stderr == ""


def test_apply_named_template_uses_declared_template(tmp_path: Path) -> None:
    hub_root, repo_root, _app_repo = _setup_apply_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/hello")

    result = apply_app_entrypoint(
        repo_root,
        "local.hello",
        template_name="followup",
        app_inputs={"goal": "follow"},
    )

    frontmatter, body = parse_markdown_frontmatter(
        result.ticket_path.read_text(encoding="utf-8")
    )
    persisted = json.loads(result.apply_inputs_path.read_text(encoding="utf-8"))

    assert result.template_name == "followup"
    assert frontmatter["agent"] == "opencode"
    assert frontmatter["title"] == "Follow-up Ticket"
    assert "# Follow-up" in body
    assert persisted["template_name"] == "followup"
