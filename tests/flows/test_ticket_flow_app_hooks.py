from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.apps import install_app
from codex_autorunner.core.config import CONFIG_FILENAME, load_hub_config
from codex_autorunner.core.flows.controller import FlowController
from codex_autorunner.core.flows.definition import FlowDefinition, StepOutcome
from codex_autorunner.core.flows.models import FlowEventType, FlowRunRecord
from codex_autorunner.core.git_utils import run_git
from codex_autorunner.core.state_roots import resolve_repo_flows_db_path
from codex_autorunner.tickets.outbox import ensure_outbox_dirs, resolve_outbox_paths
from codex_autorunner.tickets.runner_post_turn import reconcile_post_turn


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
) -> None:
    app_root = app_repo / "apps" / slug
    (app_root / "scripts").mkdir(parents=True, exist_ok=True)
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
        "hooks": {
            hook_point: [
                {
                    "tool": "run-hook",
                    "when": when,
                    "failure": failure,
                }
            ]
        },
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
    sys.exit(9)
""",
        encoding="utf-8",
    )


def _setup_workspace(tmp_path: Path) -> tuple[Path, Path]:
    hub_root = tmp_path / "hub"
    repo_root = tmp_path / "repo"
    app_repo = tmp_path / "app_repo"
    seed_hub_files(hub_root, force=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    _init_repo(app_repo)
    _configure_apps_repo(hub_root, app_repo)
    return hub_root, repo_root


def _install_fixture_app(
    hub_root: Path,
    repo_root: Path,
    app_repo: Path,
    *,
    slug: str,
    app_id: str,
    hook_point: str,
    when: dict,
    failure: str = "warn",
    mode: str = "record",
) -> None:
    _write_hook_app(
        app_repo,
        slug=slug,
        app_id=app_id,
        hook_point=hook_point,
        when=when,
        failure=failure,
        mode=mode,
    )
    _commit_repo(app_repo, f"add {slug} app")
    hub_config = load_hub_config(hub_root)
    install_app(hub_config, hub_root, repo_root, f"local:apps/{slug}")


def _read_hook_runs(repo_root: Path, app_id: str) -> list[dict]:
    path = (
        repo_root / ".codex-autorunner" / "apps" / app_id / "state" / "hook_runs.jsonl"
    )
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_ticket(repo_root: Path, *, app_value: str) -> Path:
    ticket_path = repo_root / ".codex-autorunner" / "tickets" / "TICKET-001-hook.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text(
        f"""---
ticket_id: "tkt_hook"
agent: "codex"
done: true
app: "{app_value}"
---
body
""",
        encoding="utf-8",
    )
    return ticket_path


def _reconcile_ticket_done(
    repo_root: Path,
    ticket_path: Path,
    *,
    state: dict | None = None,
    emit_event=None,
):
    outbox_paths = resolve_outbox_paths(workspace_root=repo_root, run_id="run-123")
    ensure_outbox_dirs(outbox_paths)
    result = SimpleNamespace(
        text="done",
        agent_id="codex",
        conversation_id="conv-1",
        turn_id="turn-1",
    )
    return reconcile_post_turn(
        state=state
        or {"current_ticket": str(ticket_path), "current_ticket_id": "tkt_hook"},
        workspace_root=repo_root,
        run_id="run-123",
        repo_id="repo-1",
        outbox_paths=outbox_paths,
        current_ticket_id="tkt_hook",
        current_ticket_path=str(ticket_path.relative_to(repo_root)),
        current_ticket_path_obj=ticket_path,
        canonical_agent_id="codex",
        current_ticket_profile=None,
        result=result,
        total_turns=1,
        head_before_turn=None,
        repo_fingerprint_before_turn=None,
        git_state_after={
            "repo_fingerprint_after": "fingerprint",
            "head_after_turn": None,
            "clean_after_turn": True,
            "status_after_turn": "",
            "agent_committed_this_turn": False,
        },
        lint_errors=[],
        lint_retries=0,
        commit_pending=False,
        commit_retries=0,
        max_lint_retries=3,
        max_commit_retries=2,
        auto_commit=False,
        checkpoint_message_template="checkpoint {run_id}",
        emit_event=emit_event,
    )


def test_after_ticket_hook_fires_once_for_completed_app_ticket(tmp_path: Path) -> None:
    hub_root, repo_root = _setup_workspace(tmp_path)
    app_repo = tmp_path / "app_repo"
    _install_fixture_app(
        hub_root,
        repo_root,
        app_repo,
        slug="after-ticket",
        app_id="local.after-ticket",
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": "local.after-ticket"}},
    )
    ticket_path = _write_ticket(repo_root, app_value="local.after-ticket")
    events: list[tuple[FlowEventType, dict]] = []

    result = _reconcile_ticket_done(
        repo_root,
        ticket_path,
        emit_event=lambda event_type, payload: events.append((event_type, payload)),
    )

    assert result.status == "continue"
    runs = _read_hook_runs(repo_root, "local.after-ticket")
    assert len(runs) == 1
    assert runs[0]["ticket_id"] == "tkt_hook"
    assert [item[0] for item in events][-2:] == [
        FlowEventType.APP_HOOK_STARTED,
        FlowEventType.APP_HOOK_RESULT,
    ]


def test_non_matching_ticket_frontmatter_does_not_fire(tmp_path: Path) -> None:
    hub_root, repo_root = _setup_workspace(tmp_path)
    app_repo = tmp_path / "app_repo"
    _install_fixture_app(
        hub_root,
        repo_root,
        app_repo,
        slug="non-match",
        app_id="local.non-match",
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": "local.non-match"}},
    )
    ticket_path = _write_ticket(repo_root, app_value="other.app")

    result = _reconcile_ticket_done(repo_root, ticket_path)

    assert result.status == "continue"
    assert _read_hook_runs(repo_root, "local.non-match") == []


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [("warn", "continue"), ("pause", "paused"), ("fail", "failed")],
)
def test_after_ticket_hook_warn_pause_fail_behavior(
    tmp_path: Path, failure: str, expected_status: str
) -> None:
    hub_root, repo_root = _setup_workspace(tmp_path)
    app_repo = tmp_path / "app_repo"
    app_id = f"local.{failure}-policy"
    _install_fixture_app(
        hub_root,
        repo_root,
        app_repo,
        slug=f"{failure}-policy",
        app_id=app_id,
        hook_point="after_ticket_done",
        when={"ticket_frontmatter": {"app": app_id}},
        failure=failure,
        mode="fail",
    )
    ticket_path = _write_ticket(repo_root, app_value=app_id)

    result = _reconcile_ticket_done(repo_root, ticket_path)

    assert result.status == expected_status
    assert len(_read_hook_runs(repo_root, app_id)) == 1
    if expected_status != "continue":
        assert result.reason is not None
        assert result.reason_details is not None


@pytest.mark.asyncio
async def test_after_flow_terminal_hook_fires_on_completed_run(tmp_path: Path) -> None:
    hub_root, repo_root = _setup_workspace(tmp_path)
    app_repo = tmp_path / "app_repo"
    _install_fixture_app(
        hub_root,
        repo_root,
        app_repo,
        slug="after-flow",
        app_id="local.after-flow",
        hook_point="after_flow_terminal",
        when={"status": "completed"},
    )

    async def _step(_record: FlowRunRecord, _input_data: dict) -> StepOutcome:
        return StepOutcome.complete(output={"ok": True})

    controller = FlowController(
        definition=FlowDefinition(
            flow_type="flow_hooks_test",
            name="Flow Hooks Test",
            initial_step="ticket_turn",
            steps={"ticket_turn": _step},
        ),
        db_path=resolve_repo_flows_db_path(repo_root),
        artifacts_root=repo_root / ".codex-autorunner" / "flow-artifacts",
    )
    controller.initialize()
    try:
        record = await controller.start_flow({})
        finished = await controller.run_flow(record.id)
        assert finished.status.value == "completed"
        runs = _read_hook_runs(repo_root, "local.after-flow")
        assert len(runs) == 1
        assert runs[0]["flow_run_id"] == record.id
        events = controller.get_events(record.id)
        event_types = [event.event_type for event in events]
        assert FlowEventType.APP_HOOK_STARTED in event_types
        assert FlowEventType.APP_HOOK_RESULT in event_types
    finally:
        controller.shutdown()


def test_no_installed_apps_is_noop(tmp_path: Path) -> None:
    _hub_root, repo_root = _setup_workspace(tmp_path)
    ticket_path = _write_ticket(repo_root, app_value="no.apps")

    result = _reconcile_ticket_done(repo_root, ticket_path)

    assert result.status == "continue"
