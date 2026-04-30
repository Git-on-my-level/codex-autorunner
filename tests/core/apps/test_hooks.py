from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import (
    AppHookPoint,
    InstalledAppHook,
    execute_app_archive_cleanup_hooks,
    execute_matching_installed_app_hooks,
    install_app,
    list_installed_app_hooks,
    matches_installed_app_hook,
)
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.tickets.files import read_ticket_frontmatter


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], repo_path, check=True)
    run_git(["config", "user.email", "test@example.com"], repo_path, check=True)
    run_git(["config", "user.name", "Test User"], repo_path, check=True)
    run_git(["checkout", "-b", "main"], repo_path, check=True)


def _commit_repo(repo_path: Path, message: str) -> None:
    run_git(["add", "."], repo_path, check=True)
    run_git(["commit", "-m", message], repo_path, check=True)


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


def _write_hook_app(
    app_repo: Path,
    *,
    slug: str,
    app_id: str,
    hook_point: str,
    when: dict,
    failure: str = "warn",
    mode: str = "record",
    cleanup_paths: list[str] | None = None,
) -> None:
    app_root = app_repo / "apps" / slug
    (app_root / "scripts").mkdir(parents=True, exist_ok=True)
    hook_entry = {
        "tool": "run-hook",
        "when": when,
        "failure": failure,
    }
    if cleanup_paths is not None:
        hook_entry["cleanup_paths"] = cleanup_paths
    manifest = {
        "schema_version": 1,
        "id": app_id,
        "name": f"{slug} app",
        "version": "1.0.0",
        "tools": {
            "run-hook": {
                "description": "Hook fixture tool.",
                "argv": ["python3", "scripts/hook.py", mode],
                "timeout_seconds": 5,
            }
        },
        "hooks": {hook_point: [hook_entry]},
    }
    (app_root / "car-app.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (app_root / "scripts" / "hook.py").write_text(
        """import json
import os
import sys
from pathlib import Path

mode = sys.argv[1]
state_dir = Path(os.environ["CAR_APP_STATE_DIR"])
state_dir.mkdir(parents=True, exist_ok=True)
payload = {
    "app_id": os.environ.get("CAR_APP_ID"),
    "hook_point": os.environ.get("CAR_HOOK_POINT"),
    "flow_run_id": os.environ.get("CAR_FLOW_RUN_ID"),
    "ticket_id": os.environ.get("CAR_TICKET_ID"),
    "ticket_path": os.environ.get("CAR_TICKET_PATH"),
}
with (state_dir / "hook_runs.jsonl").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\\n")
if mode == "fail":
    print("forced failure", file=sys.stderr)
    sys.exit(7)
""",
        encoding="utf-8",
    )


def _setup_installed_app(tmp_path: Path, **app_kwargs) -> tuple[Path, str]:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    app_repo = tmp_path / "app_repo"

    seed_hub_files(hub_root, force=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    _write_hook_app(app_repo, **app_kwargs)
    _commit_repo(app_repo, f"add {app_kwargs['slug']} app")

    hub_config = load_hub_config(hub_root)
    install_app(
        hub_config,
        hub_root,
        repo_root,
        f"local:apps/{app_kwargs['slug']}",
    )
    return repo_root, app_kwargs["app_id"]


def _ticket_frontmatter(tmp_path: Path, raw: str):
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text(raw, encoding="utf-8")
    frontmatter, errors = read_ticket_frontmatter(ticket_path)
    assert not errors
    assert frontmatter is not None
    return ticket_path, frontmatter


def _read_hook_runs(repo_root: Path, app_id: str) -> list[dict]:
    path = (
        repo_root / ".codex-autorunner" / "apps" / app_id / "state" / "hook_runs.jsonl"
    )
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_selector_matching_uses_frontmatter_extra_and_status(tmp_path: Path) -> None:
    repo_root, _app_id = _setup_installed_app(
        tmp_path,
        slug="selector",
        app_id="local.selector",
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": "local.selector", "done": True}},
    )
    hooks = list_installed_app_hooks(repo_root, AppHookPoint.AFTER_TICKET_DONE)
    assert len(hooks) == 1

    _ticket_path, frontmatter = _ticket_frontmatter(
        tmp_path,
        """---
ticket_id: "tkt_selector"
agent: "codex"
done: true
app: "local.selector"
---
body
""",
    )
    assert matches_installed_app_hook(hooks[0], ticket_frontmatter=frontmatter)
    assert not matches_installed_app_hook(
        hooks[0],
        ticket_frontmatter=SimpleNamespace(extra={"app": "other.app"}, done=True),
    )
    status_hook = InstalledAppHook(
        app_id="local.status",
        tool_id="run-hook",
        hook_point=AppHookPoint.AFTER_FLOW_TERMINAL,
        failure="warn",
        when={"status": "completed"},
    )
    assert matches_installed_app_hook(status_hook, flow_status="completed")
    assert not matches_installed_app_hook(status_hook, flow_status="failed")


def test_archive_cleanup_hook_removes_only_declared_app_runtime_paths(
    tmp_path: Path,
) -> None:
    repo_root, app_id = _setup_installed_app(
        tmp_path,
        slug="cleanup",
        app_id="local.cleanup",
        hook_point="after_flow_archive",
        when={"status": "completed"},
        cleanup_paths=[
            "state/run.json",
            "state/iterations.jsonl",
            "artifacts/summary.md",
            "artifacts/missing.md",
        ],
    )
    app_root = repo_root / ".codex-autorunner" / "apps" / app_id
    (app_root / "state" / "run.json").write_text("{}", encoding="utf-8")
    (app_root / "state" / "iterations.jsonl").write_text("", encoding="utf-8")
    (app_root / "artifacts" / "summary.md").write_text("summary", encoding="utf-8")

    result = execute_app_archive_cleanup_hooks(
        repo_root,
        flow_run_id="run-cleanup",
        flow_status="completed",
    )

    assert result.failed is False
    assert len(result.entries) == 1
    assert sorted(result.entries[0].removed_paths) == [
        "artifacts/summary.md",
        "state/iterations.jsonl",
        "state/run.json",
    ]
    assert result.entries[0].missing_paths == ("artifacts/missing.md",)
    assert not (app_root / "state" / "run.json").exists()
    assert not (app_root / "state" / "iterations.jsonl").exists()
    assert not (app_root / "artifacts" / "summary.md").exists()
    assert (app_root / "bundle" / "car-app.yaml").exists()


def test_archive_cleanup_hook_rejects_paths_outside_runtime_roots(
    tmp_path: Path,
) -> None:
    repo_root, app_id = _setup_installed_app(
        tmp_path,
        slug="cleanup-escape",
        app_id="local.cleanup-escape",
        hook_point="after_flow_archive",
        when={"status": "completed"},
        cleanup_paths=["bundle/car-app.yaml"],
    )
    app_root = repo_root / ".codex-autorunner" / "apps" / app_id

    result = execute_app_archive_cleanup_hooks(
        repo_root,
        flow_run_id="run-cleanup",
        flow_status="completed",
    )

    assert result.failed is True
    assert len(result.entries) == 1
    assert "state/, artifacts/, or logs/" in (result.entries[0].error or "")
    assert (app_root / "bundle" / "car-app.yaml").exists()


def test_archive_cleanup_hook_rejects_globs_matching_runtime_root(
    tmp_path: Path,
) -> None:
    repo_root, app_id = _setup_installed_app(
        tmp_path,
        slug="cleanup-root-glob",
        app_id="local.cleanup-root-glob",
        hook_point="after_flow_archive",
        when={"status": "completed"},
        cleanup_paths=["state/**"],
    )
    app_root = repo_root / ".codex-autorunner" / "apps" / app_id
    (app_root / "state" / "run.json").write_text("{}", encoding="utf-8")

    result = execute_app_archive_cleanup_hooks(
        repo_root,
        flow_run_id="run-cleanup",
        flow_status="completed",
    )

    assert result.failed is True
    assert len(result.entries) == 1
    assert "must not target a runtime root" in (result.entries[0].error or "")
    assert (app_root / "state").is_dir()
    assert (app_root / "state" / "run.json").exists()


def test_execute_matching_installed_app_hooks_runs_matching_tool(
    tmp_path: Path,
) -> None:
    repo_root, app_id = _setup_installed_app(
        tmp_path,
        slug="matcher",
        app_id="local.matcher",
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": "local.matcher"}},
    )
    ticket_path, frontmatter = _ticket_frontmatter(
        tmp_path,
        """---
ticket_id: "tkt_matcher"
agent: "codex"
done: true
app: "local.matcher"
---
body
""",
    )
    events: list[tuple[FlowEventType, dict]] = []

    result = execute_matching_installed_app_hooks(
        repo_root,
        "after_ticket_done",
        flow_run_id="run-123",
        ticket_id="tkt_matcher",
        ticket_path=ticket_path,
        ticket_frontmatter=frontmatter,
        emit_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    runs = _read_hook_runs(repo_root, app_id)
    assert result.paused is False
    assert result.failed is False
    assert len(runs) == 1
    assert runs[0]["ticket_id"] == "tkt_matcher"
    assert runs[0]["flow_run_id"] == "run-123"
    assert runs[0]["hook_point"] == "after_ticket_done"
    assert [item[0] for item in events] == [
        FlowEventType.APP_HOOK_STARTED,
        FlowEventType.APP_HOOK_RESULT,
    ]


@pytest.mark.parametrize(
    ("failure", "expected_pause", "expected_fail"),
    [("warn", False, False), ("pause", True, False), ("fail", False, True)],
)
def test_execute_matching_installed_app_hooks_honors_failure_policy(
    tmp_path: Path,
    failure: str,
    expected_pause: bool,
    expected_fail: bool,
) -> None:
    repo_root, app_id = _setup_installed_app(
        tmp_path,
        slug=f"policy-{failure}",
        app_id=f"local.policy-{failure}",
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": f"local.policy-{failure}"}},
        failure=failure,
        mode="fail",
    )
    ticket_path, frontmatter = _ticket_frontmatter(
        tmp_path,
        f"""---
ticket_id: "tkt_{failure}"
agent: "codex"
done: true
app: "local.policy-{failure}"
---
body
""",
    )

    result = execute_matching_installed_app_hooks(
        repo_root,
        "after_ticket_done",
        flow_run_id="run-policy",
        ticket_id=f"tkt_{failure}",
        ticket_path=ticket_path,
        ticket_frontmatter=frontmatter,
    )

    runs = _read_hook_runs(repo_root, app_id)
    assert len(runs) == 1
    assert result.paused is expected_pause
    assert result.failed is expected_fail
    if failure == "warn":
        assert result.reason is None
    else:
        assert result.reason is not None
        assert result.reason_details is not None
