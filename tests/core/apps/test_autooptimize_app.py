from __future__ import annotations

from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.apps import (
    apply_app_entrypoint,
    get_installed_app,
    install_app,
    run_installed_app_tool,
)
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
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


def _configure_blessed_apps_repo(hub_root: Path, app_repo: Path) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "blessed",
                "url": str(app_repo),
                "trusted": True,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _write_autooptimize_fixture(app_repo: Path) -> None:
    app_root = app_repo / "apps" / "autooptimize"
    (app_root / "templates").mkdir(parents=True, exist_ok=True)
    (app_root / "scripts").mkdir(exist_ok=True)
    (app_root / "car-app.yaml").write_text(
        """schema_version: 1
id: blessed.autooptimize
name: AutoOptimize
version: 0.1.0
description: Test fixture for the blessed AutoOptimize distribution path.
entrypoint:
  template: templates/bootstrap.md
inputs:
  goal:
    required: true
    description: Optimization goal.
templates:
  iteration:
    path: templates/iteration.md
    description: Run one measurable optimization hypothesis.
tools:
  status:
    argv: ["python3", "scripts/status.py"]
""",
        encoding="utf-8",
    )
    (app_root / "templates" / "bootstrap.md").write_text(
        """---
agent: codex
done: false
title: AutoOptimize Bootstrap
---

# AutoOptimize Bootstrap

Goal: {{ goal }}
""",
        encoding="utf-8",
    )
    (app_root / "templates" / "iteration.md").write_text(
        """---
agent: opencode
done: false
title: AutoOptimize Iteration
---

# AutoOptimize Iteration

Run one measurable iteration for {{ goal }}.
""",
        encoding="utf-8",
    )
    (app_root / "scripts" / "status.py").write_text(
        "print('autooptimize ok')\n", encoding="utf-8"
    )


def _setup_blessed_autooptimize_env(tmp_path: Path) -> tuple[Path, Path, str]:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = tmp_path / "repo"
    seed_repo_files(repo_root, git_required=False)

    app_repo = tmp_path / "blessed-car-apps"
    _init_repo(app_repo)
    _configure_blessed_apps_repo(hub_root, app_repo)
    _write_autooptimize_fixture(app_repo)
    commit_sha = _commit_repo(app_repo, "add autooptimize app")

    return hub_root, repo_root, commit_sha


def test_blessed_autooptimize_installs_applies_and_runs_from_migrated_source_path(
    tmp_path: Path,
) -> None:
    hub_root, repo_root, commit_sha = _setup_blessed_autooptimize_env(tmp_path)
    hub_config = load_hub_config(hub_root)

    install_result = install_app(
        hub_config, hub_root, repo_root, "blessed:apps/autooptimize"
    )

    assert install_result.app.app_id == "blessed.autooptimize"
    assert install_result.app.lock.source_repo_id == "blessed"
    assert install_result.app.lock.source_path == "apps/autooptimize"
    assert install_result.app.lock.source_ref == "main"
    assert install_result.app.lock.commit_sha == commit_sha
    assert install_result.app.lock.trusted is True
    assert install_result.app.bundle_verified is True

    apply_result = apply_app_entrypoint(
        repo_root,
        "blessed.autooptimize",
        template_name="iteration",
        app_inputs={"goal": "Reduce p95 latency"},
    )
    frontmatter, body = parse_markdown_frontmatter(
        apply_result.ticket_path.read_text(encoding="utf-8")
    )

    assert frontmatter["app"] == "blessed.autooptimize"
    assert frontmatter["app_source"] == "blessed:apps/autooptimize@main"
    assert "Reduce p95 latency" in body

    tool_result = run_installed_app_tool(repo_root, "blessed.autooptimize", "status")

    assert tool_result.exit_code == 0
    assert "autooptimize ok" in tool_result.stdout_excerpt

    reloaded = get_installed_app(repo_root, "blessed.autooptimize")
    assert reloaded is not None
    assert reloaded.bundle_verified is True
