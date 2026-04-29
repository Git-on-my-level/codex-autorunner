from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import (
    apply_app_entrypoint,
    get_installed_app,
    install_app,
    list_installed_app_tools,
    run_installed_app_tool,
)
from codex_autorunner.core.apps.hooks import execute_matching_installed_app_hooks
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.state_roots import resolve_repo_state_root
from codex_autorunner.tickets.frontmatter import parse_markdown_frontmatter

AUTOOPTIMIZE_APP_DIR = Path(__file__).resolve().parents[3] / "apps" / "autooptimize"


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


def _configure_apps_repo(hub_root: Path, app_repo: Path) -> None:
    config_path = hub_root / CONFIG_FILENAME
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["apps"] = {
        "enabled": True,
        "repos": [
            {
                "id": "local",
                "url": str(app_repo),
                "trusted": True,
                "default_ref": "main",
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _copy_app_to_repo(app_repo: Path) -> None:
    shutil.copytree(
        AUTOOPTIMIZE_APP_DIR,
        app_repo / "apps" / "autooptimize",
        dirs_exist_ok=True,
    )


def _setup_autooptimize_env(tmp_path: Path) -> tuple[Path, Path]:
    hub_root = tmp_path / "hub"
    seed_hub_files(hub_root, force=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    app_repo = tmp_path / "app_repo"
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    _copy_app_to_repo(app_repo)
    _commit_repo(app_repo, "add autooptimize app")

    return hub_root, repo_root


def test_autooptimize_apply_named_template_uses_iteration_ticket(
    tmp_path: Path,
) -> None:
    hub_root, repo_root = _setup_autooptimize_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, "local:apps/autooptimize")

    result = apply_app_entrypoint(
        repo_root,
        "blessed.autooptimize",
        template_name="iteration",
        app_inputs={"goal": "Reduce p95 latency"},
    )

    assert result.template_name == "iteration"
    assert result.ticket_path.exists()

    frontmatter, body = parse_markdown_frontmatter(
        result.ticket_path.read_text(encoding="utf-8")
    )
    assert frontmatter["agent"] == "opencode"
    assert frontmatter["app"] == "blessed.autooptimize"
    assert frontmatter["app_source"] == "local:apps/autooptimize@main"
    assert "record-iteration" in body
    assert "## App Inputs" in body


def test_autooptimize_install_run_and_after_flow_terminal_hook(tmp_path: Path) -> None:
    hub_root, repo_root = _setup_autooptimize_env(tmp_path)
    hub_config = load_hub_config(hub_root)
    install_result = install_app(
        hub_config, hub_root, repo_root, "local:apps/autooptimize"
    )

    installed = get_installed_app(repo_root, "blessed.autooptimize")
    assert installed is not None
    assert installed.bundle_verified is True

    tools = list_installed_app_tools(repo_root, "blessed.autooptimize")
    assert {tool.tool_id for tool in tools} == {
        "init-run",
        "record-baseline",
        "record-iteration",
        "render-summary-card",
        "plan-next-ticket",
        "status",
        "validate-state",
    }

    init_result = run_installed_app_tool(
        repo_root,
        "blessed.autooptimize",
        "init-run",
        extra_argv=[
            "--goal",
            "Reduce p95 latency",
            "--metric",
            "p95 latency",
            "--direction",
            "lower",
            "--unit",
            "ms",
            "--max-iterations",
            "3",
        ],
    )
    assert init_result.exit_code == 0

    baseline_result = run_installed_app_tool(
        repo_root,
        "blessed.autooptimize",
        "record-baseline",
        extra_argv=["--value", "110", "--unit", "ms", "--summary", "main baseline"],
    )
    assert baseline_result.exit_code == 0

    plan_result = run_installed_app_tool(
        repo_root,
        "blessed.autooptimize",
        "plan-next-ticket",
        extra_argv=["--json"],
    )
    assert plan_result.exit_code == 0
    assert '"template": "iteration"' in plan_result.stdout_excerpt
    assert '"next_iteration": 1' in plan_result.stdout_excerpt

    iteration_result = run_installed_app_tool(
        repo_root,
        "blessed.autooptimize",
        "record-iteration",
        extra_argv=[
            "--iteration",
            "1",
            "--ticket",
            "TICKET-301-autooptimize-iteration.md",
            "--hypothesis",
            "Cache app discovery",
            "--value",
            "91",
            "--unit",
            "ms",
            "--decision",
            "keep",
            "--guard-status",
            "pass",
            "--milestone",
            "discovery-cache",
        ],
    )
    assert iteration_result.exit_code == 0

    status_result = run_installed_app_tool(
        repo_root,
        "blessed.autooptimize",
        "status",
        extra_argv=["--json"],
    )
    assert status_result.exit_code == 0
    assert '"improvement_absolute": 19.0' in status_result.stdout_excerpt

    hook_result = execute_matching_installed_app_hooks(
        repo_root,
        "after_flow_terminal",
        flow_run_id="run-autooptimize",
        flow_status="completed",
    )
    assert hook_result.failed is False
    assert hook_result.paused is False
    assert len(hook_result.executions) == 1
    assert hook_result.executions[0].exit_code == 0

    app_root = resolve_repo_state_root(repo_root) / "apps" / install_result.app.app_id
    assert (app_root / "artifacts" / "summary.md").exists()
    assert (app_root / "artifacts" / "summary.svg").exists()

    summary_md = (app_root / "artifacts" / "summary.md").read_text(encoding="utf-8")
    assert "AutoOptimize Summary" in summary_md
    assert "discovery-cache" in summary_md
    assert not any(install_result.app.paths.bundle_root.rglob("__pycache__"))
