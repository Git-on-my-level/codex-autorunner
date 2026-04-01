import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import load_hub_config
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.hub_inbox_resolution import record_message_resolution
from codex_autorunner.core.orchestration import OrchestrationBindingStore
from codex_autorunner.core.pma_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    build_hub_snapshot,
    format_pma_prompt,
    get_active_context_auto_prune_meta,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.state import RunnerState, save_state
from codex_autorunner.manifest import load_manifest, save_manifest


def _write_hub_config(hub_root: Path, data: dict) -> None:
    """Helper to write hub config to .codex-autorunner/config.yml."""
    config_path = hub_root / ".codex-autorunner" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _seed_paused_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.PAUSED)


def _seed_completed_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.COMPLETED)


def _seed_failed_run(
    repo_root: Path,
    run_id: str,
    *,
    state: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state=state or {},
            metadata={},
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.FAILED,
            state=state or {},
            error_message=error_message,
        )


def _seed_failed_worker_dead_run(repo_root: Path, run_id: str) -> None:
    _seed_failed_run(
        repo_root,
        run_id,
        state={
            "failure": {
                "failed_at": "2026-03-21T00:00:00Z",
                "failure_reason_code": "worker_dead",
                "failure_class": "worker_dead",
            }
        },
        error_message=(
            "Worker died (status=dead, pid=38621, reason: worker PID not running, "
            "exit_code=-15)"
        ),
    )


def _seed_failed_worker_dead_legacy_run(repo_root: Path, run_id: str) -> None:
    _seed_failed_run(
        repo_root,
        run_id,
        state={"ticket_engine": {"reason_code": "worker_dead"}},
        error_message=None,
    )


def _seed_stopped_run(repo_root: Path, run_id: str) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            state={},
            metadata={},
        )
        store.update_flow_run_status(run_id, FlowRunStatus.STOPPED)


def _write_dispatch_history(
    repo_root: Path, run_id: str, seq: int, *, mode: str = "pause"
) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "DISPATCH.md").write_text(
        f"---\nmode: {mode}\ntitle: dispatch-{seq}\n---\n\nPlease review.\n",
        encoding="utf-8",
    )


def _write_ticket(repo_root: Path, ticket_name: str, *, done: bool) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / ticket_name).write_text(
        (
            "---\n"
            f"title: {ticket_name}\n"
            f"done: {'true' if done else 'false'}\n"
            "---\n\n"
            "Body\n"
        ),
        encoding="utf-8",
    )


def test_format_pma_prompt_includes_workspace_docs(tmp_path: Path) -> None:
    """Test that format_pma_prompt with hub_root includes the PMA docs block."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<pma_workspace_docs>" in result
    assert "</pma_workspace_docs>" in result


def test_format_pma_prompt_includes_agents_section(tmp_path: Path) -> None:
    """Test that AGENTS.md content is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<AGENTS_MD>" in result
    assert "</AGENTS_MD>" in result
    assert "Durable best-practices" in result


def test_format_pma_prompt_includes_active_context_section(tmp_path: Path) -> None:
    """Test that active_context.md content is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<ACTIVE_CONTEXT_MD>" in result
    assert "</ACTIVE_CONTEXT_MD>" in result
    assert "short-lived" in result


def test_format_pma_prompt_includes_budget_metadata(tmp_path: Path) -> None:
    """Test that active_context_budget metadata is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<ACTIVE_CONTEXT_BUDGET" in result
    assert "lines='200'" in result
    assert "current_lines='8'" in result
    assert "/>" in result


def test_format_pma_prompt_includes_context_log_tail(tmp_path: Path) -> None:
    """Test that context_log_tail.md section is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<CONTEXT_LOG_TAIL_MD>" in result
    assert "</CONTEXT_LOG_TAIL_MD>" in result
    assert "append-only" in result


def test_format_pma_prompt_without_hub_root(tmp_path: Path) -> None:
    """Test that format_pma_prompt without hub_root does not include PMA docs."""
    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=None)

    assert "<pma_workspace_docs>" not in result
    assert "</pma_workspace_docs>" not in result
    assert "<pma_fastpath>" in result


def test_format_pma_prompt_load_failure_still_includes_fastpath(
    tmp_path: Path,
) -> None:
    """Fastpath remains available even when PMA docs cannot be loaded."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    with patch(
        "codex_autorunner.core.pma_context.load_pma_workspace_docs",
        side_effect=RuntimeError("boom"),
    ):
        result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<pma_workspace_docs>" not in result
    assert "</pma_workspace_docs>" not in result
    assert "<pma_fastpath>" in result


def test_truncation_applied_to_long_agents(tmp_path: Path) -> None:
    """Test that long AGENTS.md content is truncated."""
    seed_hub_files(tmp_path, force=True)

    agents_path = tmp_path / ".codex-autorunner" / "pma" / "docs" / "AGENTS.md"
    long_content = "x" * 2000
    agents_path.write_text(long_content, encoding="utf-8")

    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 100,
                "active_context_max_lines": 200,
                "context_log_tail_lines": 120,
            },
        },
    )

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert len(result) > 0
    assert "..." in result


def test_truncation_applied_to_long_active_context(tmp_path: Path) -> None:
    """Test that long active_context.md content is truncated."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    long_content = "y" * 2000
    active_context_path.write_text(long_content, encoding="utf-8")

    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 100,
                "active_context_max_lines": 200,
                "context_log_tail_lines": 120,
            },
        },
    )

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert len(result) > 0
    assert "..." in result


def test_context_log_tail_lines(tmp_path: Path) -> None:
    """Test that only the last N lines of context_log.md are injected."""
    seed_hub_files(tmp_path, force=True)

    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )
    log_lines = ["line 1", "line 2", "line 3", "line 4", "line 5"]
    context_log_path.write_text("\n".join(log_lines), encoding="utf-8")

    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 12000,
                "active_context_max_lines": 200,
                "context_log_tail_lines": 3,
            },
        },
    )

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<CONTEXT_LOG_TAIL_MD>" in result
    assert "line 3" in result
    assert "line 4" in result
    assert "line 5" in result
    assert "line 1" not in result
    assert "line 2" not in result


def test_context_log_tail_lines_one(tmp_path: Path) -> None:
    """Test that context_log_tail with 1 line only includes the last line."""
    # Write config before seeding to ensure it takes effect
    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 12000,
                "active_context_max_lines": 200,
                "context_log_tail_lines": 1,
            },
        },
    )

    # Seed files with force=False to not overwrite config
    seed_hub_files(tmp_path, force=False)

    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )
    log_lines = ["line 1", "line 2", "line 3"]
    context_log_path.write_text("\n".join(log_lines), encoding="utf-8")

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<CONTEXT_LOG_TAIL_MD>" in result
    assert "</CONTEXT_LOG_TAIL_MD>" in result
    # Extract just the context_log_tail section
    start_idx = result.find("<CONTEXT_LOG_TAIL_MD>")
    end_idx = result.find("</CONTEXT_LOG_TAIL_MD>")
    context_section = result[start_idx : end_idx + len("</CONTEXT_LOG_TAIL_MD>")]
    # With 1 tail line, only the last line should be present
    assert "line 3" in context_section
    assert "line 1" not in context_section
    assert "line 2" not in context_section


def test_format_pma_prompt_includes_hub_snapshot_and_message(tmp_path: Path) -> None:
    """Test that hub_snapshot and user_message sections are always included."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [
            {
                "repo_id": "repo-1",
                "run_id": "run-9",
                "seq": 3,
                "dispatch": {
                    "mode": "pause",
                    "is_handoff": True,
                    "title": "Need input",
                    "body": "Please respond",
                },
                "files": ["request.md", "log.txt"],
                "open_url": "https://example.invalid/run/9",
            }
        ]
    }
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<hub_snapshot>" in result
    assert "Run Dispatches (paused runs needing attention):" in result
    assert "Ticket planning constraints (state machine):" in result
    assert "Managed threads vs ticket flows:" in result
    assert "car pma thread spawn" in result
    assert "Automation continuity (subscriptions + timers):" in result
    assert "/hub/pma/subscriptions" in result
    assert "/hub/pma/timers" in result
    assert "active_context.md" in result
    assert "decisions.md" in result
    assert "spec.md" in result
    assert "repo_id=repo-1" in result
    assert "run_id=run-9" in result
    assert "mode=pause" in result
    assert "handoff=true" in result
    assert "title: Need input" in result
    assert "body: Please respond" in result
    assert "attachments: [request.md, log.txt]" in result
    assert "open_url: https://example.invalid/run/9" in result
    assert "</hub_snapshot>" in result
    assert "<user_message>" in result
    assert "User message" in result
    assert "</user_message>" in result


def test_format_pma_prompt_with_custom_agent_content(tmp_path: Path) -> None:
    """Test that custom AGENTS.md content is preserved in the prompt."""
    seed_hub_files(tmp_path, force=True)

    agents_path = tmp_path / ".codex-autorunner" / "pma" / "docs" / "AGENTS.md"
    custom_content = "# Custom AGENTS\n\nThis is custom content."
    agents_path.write_text(custom_content, encoding="utf-8")

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "Custom AGENTS" in result
    assert "This is custom content" in result


def test_active_context_line_count_reflected_in_metadata(tmp_path: Path) -> None:
    """Test that the line count is correctly reflected in the budget metadata."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    custom_content = "line 1\nline 2\nline 3"
    active_context_path.write_text(custom_content, encoding="utf-8")

    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 12000,
                "active_context_max_lines": 200,
                "context_log_tail_lines": 120,
            },
        },
    )

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "current_lines='3'" in result


def test_format_pma_prompt_auto_prunes_active_context_when_over_budget(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )
    long_content = "\n".join(f"line {idx}" for idx in range(260))
    active_context_path.write_text(long_content, encoding="utf-8")

    _write_hub_config(
        tmp_path,
        {
            "mode": "hub",
            "pma": {
                "docs_max_chars": 12000,
                "active_context_max_lines": 50,
                "context_log_tail_lines": 120,
            },
        },
    )

    result = format_pma_prompt(
        "Base prompt", {"test": "data"}, "hello", hub_root=tmp_path
    )

    pruned_active = active_context_path.read_text(encoding="utf-8")
    assert "Auto-pruned on" in pruned_active
    assert "line 259" not in pruned_active

    log_content = context_log_path.read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert "line 259" in log_content

    meta = get_active_context_auto_prune_meta(tmp_path)
    assert meta is not None
    assert meta["line_count_before"] == 260
    assert meta["line_budget"] == 50

    assert "<ACTIVE_CONTEXT_AUTO_PRUNE" in result
    assert "triggered_now='true'" in result


def test_get_active_context_auto_prune_meta_normalizes_invalid_state_fields(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)
    state_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / ".active_context_state.json"
    )
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_auto_pruned_at": " 2026-03-02T00:00:00Z ",
                "line_count_before": "invalid",
                "line_budget": "invalid",
            }
        ),
        encoding="utf-8",
    )

    meta = get_active_context_auto_prune_meta(tmp_path)

    assert meta is not None
    assert meta["last_auto_pruned_at"] == "2026-03-02T00:00:00Z"
    assert meta["line_count_before"] == 0
    assert meta["line_budget"] == PMA_ACTIVE_CONTEXT_MAX_LINES


def test_build_hub_snapshot_includes_templates(tmp_path: Path) -> None:
    """Verify templates metadata is included in hub snapshots."""
    seed_hub_files(tmp_path, force=True)

    config_path = tmp_path / ".codex-autorunner" / "config.yml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["templates"] = {
        "enabled": True,
        "repos": [
            {
                "id": "alpha",
                "url": "https://example.com/alpha.git",
                "trusted": True,
                "default_ref": "main",
            },
            {
                "id": "beta",
                "url": "https://example.com/beta.git",
                "trusted": False,
                "default_ref": "stable",
            },
        ],
    }
    config_path.write_text(
        yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8"
    )

    supervisor = HubSupervisor.from_path(tmp_path)
    try:
        snapshot = asyncio.run(build_hub_snapshot(supervisor, hub_root=tmp_path))
    finally:
        supervisor.shutdown()

    templates = snapshot.get("templates")
    assert isinstance(templates, dict)
    assert templates.get("enabled") is True
    repos = templates.get("repos")
    assert isinstance(repos, list)
    assert repos[0]["id"] == "alpha"
    assert repos[0]["trusted"] is True
    assert repos[0]["default_ref"] == "main"
    assert repos[1]["id"] == "beta"
    assert repos[1]["trusted"] is False
    assert repos[1]["default_ref"] == "stable"
    assert "url" not in repos[0]


def test_build_hub_snapshot_includes_automation_summary(hub_env) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        store = supervisor.get_pma_automation_store()
        store.create_subscription(
            {
                "event_types": ["flow_completed"],
                "repo_id": hub_env.repo_id,
                "run_id": "run-1",
                "from_state": "running",
                "to_state": "completed",
                "lane_id": "pma:lane-next",
                "idempotency_key": "snapshot-sub-1",
            }
        )
        store.create_timer(
            {
                "timer_type": "one_shot",
                "delay_seconds": 60,
                "repo_id": hub_env.repo_id,
                "run_id": "run-1",
                "reason": "watchdog",
                "idempotency_key": "snapshot-timer-1",
            }
        )
        store.enqueue_wakeup(
            source="lifecycle_subscription",
            repo_id=hub_env.repo_id,
            run_id="run-1",
            from_state="running",
            to_state="completed",
            reason="flow_completed",
            timestamp="2026-01-01T00:00:00Z",
            idempotency_key="snapshot-wakeup-1",
        )

        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    automation = snapshot.get("automation")
    assert isinstance(automation, dict)
    subscriptions = automation.get("subscriptions")
    assert isinstance(subscriptions, dict)
    assert int(subscriptions.get("active_count") or 0) >= 1
    timers = automation.get("timers")
    assert isinstance(timers, dict)
    assert int(timers.get("pending_count") or 0) >= 1
    wakeups = automation.get("wakeups")
    assert isinstance(wakeups, dict)
    assert int(wakeups.get("pending_count") or 0) >= 1

    rendered = _render_hub_snapshot(snapshot)
    assert "PMA Automation:" in rendered
    assert "subscriptions_active=" in rendered


def test_build_hub_snapshot_includes_action_queue_with_supersession(hub_env) -> None:
    run_id = "12121212-3434-5656-7878-909090909090"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    inbox_dir = hub_env.hub_root / ".codex-autorunner" / "filebox" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "ticket-pack.md").write_text("ticket payload\n", encoding="utf-8")

    thread_store = PmaThreadStore(hub_env.hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="snapshot-action-queue-thread",
    )

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        supervisor.get_pma_automation_store().enqueue_wakeup(
            source="lifecycle_subscription",
            repo_id=hub_env.repo_id,
            run_id=run_id,
            reason="flow_paused",
            timestamp="2026-03-16T12:30:00Z",
            idempotency_key="snapshot-action-queue",
        )
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    queue = snapshot.get("action_queue") or []
    assert queue
    assert queue[0]["queue_source"] == "ticket_flow_inbox"
    assert queue[0]["supersession"]["status"] == "primary"

    thread_item = next(
        item
        for item in queue
        if item.get("item_type") == "managed_thread_followup_summary"
        and item.get("followup_state") == "reusable"
    )
    assert thread_item["queue_source"] == "managed_thread_followup"
    assert thread_item["followup_state"] == "reusable"
    assert thread["managed_thread_id"] in (thread_item.get("managed_thread_ids") or [])
    assert thread_item["recommended_action"] == "show_reusable_threads"
    assert thread_item["supersession"]["status"] == "superseded"
    assert thread_item["supersession"]["superseded_by"] == queue[0]["action_queue_id"]

    wakeup_item = next(
        item for item in queue if item.get("item_type") == "automation_wakeup"
    )
    assert wakeup_item["supersession"]["status"] == "superseded"
    assert wakeup_item["supersession"]["superseded_by"] == queue[0]["action_queue_id"]

    file_item = next(item for item in queue if item.get("item_type") == "pma_file")
    assert file_item["supersession"]["status"] == "non_primary"


def test_build_hub_snapshot_failed_run_queue_item_recommends_archive_with_repo(
    hub_env,
) -> None:
    run_id = "56565656-7878-9090-1212-343434343434"
    _seed_failed_run(hub_env.repo_root, run_id)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    queue = snapshot.get("action_queue") or []
    failed_item = next(item for item in queue if item.get("run_id") == run_id)
    assert failed_item["queue_source"] == "ticket_flow_inbox"
    assert failed_item["recommended_action"] == "diagnose_or_restart"
    assert failed_item["recommended_detail"] == (
        f"car flow ticket_flow archive --repo {hub_env.repo_root} --run-id {run_id}"
    )


def test_build_hub_snapshot_prefers_status_change_time_for_thread_freshness(
    hub_env,
) -> None:
    thread_store = PmaThreadStore(hub_env.hub_root)
    thread = thread_store.create_thread(
        "codex",
        hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="freshness-status-change-thread",
    )
    thread_id = thread["managed_thread_id"]

    thread_store.create_turn(thread_id, prompt="First turn")
    thread_store.mark_turn_finished(
        thread_store.get_running_turn(thread_id)["managed_turn_id"],
        status="ok",
        assistant_text="done",
    )

    with thread_store._write_conn() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            UPDATE orch_thread_targets
               SET updated_at = ?,
                   status_updated_at = ?
             WHERE thread_target_id = ?
            """,
            ("2026-03-16T12:00:00Z", "2026-03-16T09:00:00Z", thread_id),
        )
        conn.commit()

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    thread_snapshot = next(
        item
        for item in (snapshot.get("pma_threads") or [])
        if item.get("managed_thread_id") == thread_id
    )
    freshness = thread_snapshot.get("freshness") or {}
    assert freshness.get("recency_basis") == "thread_status_changed_at"
    assert freshness.get("basis_at") == "2026-03-16T09:00:00+00:00"


def test_build_hub_snapshot_includes_effective_destination(hub_env) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    hub_config = load_hub_config(hub_env.hub_root)
    manifest = load_manifest(hub_config.manifest_path, hub_env.hub_root)
    repo = manifest.get(hub_env.repo_id)
    assert repo is not None
    repo.destination = {"kind": "docker", "image": "busybox:latest"}
    save_manifest(hub_config.manifest_path, manifest, hub_env.hub_root)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    repos = snapshot.get("repos") or []
    repo_summary = next(
        (entry for entry in repos if entry.get("id") == hub_env.repo_id),
        {},
    )
    assert repo_summary
    effective_destination = repo_summary.get("effective_destination") or {}
    assert effective_destination.get("kind") == "docker"
    assert effective_destination.get("image") == "busybox:latest"

    rendered = _render_hub_snapshot(snapshot)
    assert "destination=docker:busybox:latest" in rendered


def test_build_hub_snapshot_surfaces_unreadable_latest_dispatch(hub_env) -> None:
    run_id = "66666666-6666-6666-6666-666666666666"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)
    _write_dispatch_history(
        hub_env.repo_root, run_id, seq=2, mode="invalid_mode"
    )  # parseable frontmatter, invalid dispatch mode

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    inbox = snapshot.get("inbox") or []
    assert len(inbox) == 1
    item = inbox[0]
    assert item["run_id"] == run_id
    assert item["item_type"] == "run_state_attention"
    assert item["seq"] == 2
    assert item.get("dispatch") is None
    assert "unreadable dispatch metadata" in (item.get("reason") or "").lower()
    run_state = item.get("run_state") or {}
    assert run_state.get("state") == "blocked"


def test_build_hub_snapshot_demotes_stale_paused_dispatch_when_no_tickets_remain(
    hub_env,
) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()

    run_id = "67676767-6767-6767-6767-676767676767"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1, mode="pause")
    _write_dispatch_history(hub_env.repo_root, run_id, seq=2, mode="turn_summary")

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    inbox = snapshot.get("inbox") or []
    assert len(inbox) == 1
    item = inbox[0]
    assert item["run_id"] == run_id
    assert item["item_type"] == "run_state_attention"
    assert item["next_action"] == "inspect_and_resume"
    assert item["seq"] == 1
    assert (item.get("dispatch") or {}).get("mode") == "pause"
    assert (
        "resume preflight would fail because no tickets remain"
        in (item.get("reason") or "").lower()
    )
    run_state = item.get("run_state") or {}
    assert run_state.get("state") == "blocked"
    assert run_state.get("recommended_action", "").endswith("--force")


def test_build_hub_snapshot_suppresses_stale_failed_worker_dead_run_when_no_tickets_remain(
    hub_env,
) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()

    run_id = "68686868-6868-6868-6868-686868686868"
    _seed_failed_worker_dead_run(hub_env.repo_root, run_id)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    assert (snapshot.get("inbox") or []) == []
    assert (snapshot.get("action_queue") or []) == []
    repos = snapshot.get("repos") or []
    repo_entry = next(repo for repo in repos if repo.get("id") == hub_env.repo_id)
    canonical = repo_entry.get("canonical_state_v1") or {}
    assert canonical.get("latest_run_id") == run_id
    assert canonical.get("latest_run_status") == "failed"


def test_build_hub_snapshot_suppresses_stale_failed_worker_dead_legacy_run_when_no_tickets_remain(
    hub_env,
) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()

    run_id = "69696969-6969-6969-6969-696969696969"
    _seed_failed_worker_dead_legacy_run(hub_env.repo_root, run_id)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    assert (snapshot.get("inbox") or []) == []
    assert (snapshot.get("action_queue") or []) == []


def test_build_hub_snapshot_filters_dismissed_stopped_run_from_inbox_and_queue(
    hub_env,
) -> None:
    run_id = "6a6a6a6a-6a6a-6a6a-6a6a-6a6a6a6a6a6a"
    _write_ticket(hub_env.repo_root, "TICKET-001.md", done=False)
    _seed_stopped_run(hub_env.repo_root, run_id)
    record_message_resolution(
        repo_root=hub_env.repo_root,
        repo_id=hub_env.repo_id,
        run_id=run_id,
        item_type="run_stopped",
        seq=None,
        action="dismiss",
        reason="superseded by newer authoritative run",
        actor="test",
    )

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    assert (snapshot.get("inbox") or []) == []
    assert (snapshot.get("action_queue") or []) == []


def test_build_hub_snapshot_keeps_stopped_run_visible_when_no_tickets_remain(
    hub_env,
) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()

    run_id = "7a7a7a7a-7a7a-7a7a-7a7a-7a7a7a7a7a7a"
    _seed_stopped_run(hub_env.repo_root, run_id)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    inbox = snapshot.get("inbox") or []
    assert len(inbox) == 1
    assert inbox[0]["run_id"] == run_id
    assert inbox[0]["item_type"] == "run_stopped"
    assert inbox[0]["next_action"] == "diagnose_or_restart"

    queue = snapshot.get("action_queue") or []
    assert len(queue) == 1
    assert queue[0]["run_id"] == run_id
    assert queue[0]["queue_source"] == "ticket_flow_inbox"


def test_build_hub_snapshot_repo_entries_include_canonical_state_v1(hub_env) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()
    _write_ticket(hub_env.repo_root, "TICKET-001.md", done=False)
    _write_ticket(hub_env.repo_root, "TICKET-002.md", done=True)

    run_id = "16161616-1616-1616-1616-161616161616"
    _seed_paused_run(hub_env.repo_root, run_id)
    _write_dispatch_history(hub_env.repo_root, run_id, seq=1)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    repos = snapshot.get("repos") or []
    repo_entry = next(repo for repo in repos if repo.get("id") == hub_env.repo_id)
    canonical = repo_entry.get("canonical_state_v1") or {}

    assert canonical.get("schema_version") == 1
    assert canonical.get("repo_id") == hub_env.repo_id
    assert canonical.get("repo_root") == str(hub_env.repo_root)
    assert canonical.get("ingested") is True
    assert canonical.get("ingest_source") == "ticket_files"
    assert canonical.get("frontmatter_total_count") == 2
    assert canonical.get("frontmatter_done_count") == 1
    assert canonical.get("effective_next_ticket") == "TICKET-001.md"
    assert canonical.get("latest_run_id") == run_id
    assert canonical.get("latest_run_status") == "paused"
    assert canonical.get("state") == "paused"
    assert canonical.get("attention_required") is True
    assert isinstance(canonical.get("recommended_actions"), list)
    assert canonical.get("recommended_action")
    assert canonical.get("recommendation_confidence") in {"high", "medium", "low"}
    assert canonical.get("observed_at")
    assert canonical.get("recommendation_generated_at")
    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at")
    assert freshness.get("recency_basis")
    assert freshness.get("basis_at")
    assert isinstance(freshness.get("is_stale"), bool)

    assert snapshot.get("generated_at")
    snapshot_freshness = snapshot.get("freshness") or {}
    assert snapshot_freshness.get("generated_at")
    repos_section = (snapshot_freshness.get("sections") or {}).get("repos") or {}
    assert repos_section.get("entity_count") >= 1


def test_build_hub_snapshot_marks_stale_start_new_flow_recommendations(hub_env) -> None:
    ticket_dir = hub_env.repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    for ticket in ticket_dir.glob("TICKET-*.md"):
        ticket.unlink()
    _write_ticket(hub_env.repo_root, "TICKET-001.md", done=False)

    run_id = "17171717-1717-1717-1717-171717171717"
    _seed_completed_run(hub_env.repo_root, run_id)

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    repos = snapshot.get("repos") or []
    repo_entry = next(repo for repo in repos if repo.get("id") == hub_env.repo_id)
    canonical = repo_entry.get("canonical_state_v1") or {}

    assert canonical.get("latest_run_id") == run_id
    assert canonical.get("latest_run_status") == "completed"
    assert canonical.get("effective_next_ticket") == "TICKET-001.md"
    assert "ticket_flow start" in (canonical.get("recommended_action") or "")
    assert canonical.get("recommendation_stale_reason")
    assert canonical.get("recommendation_confidence") == "low"
    freshness = canonical.get("freshness") or {}
    assert freshness.get("generated_at")
    assert freshness.get("recency_basis")


def test_build_hub_snapshot_clears_stale_exit_code_when_last_run_id_is_rewritten(
    hub_env,
) -> None:
    _seed_failed_run(hub_env.repo_root, "older-failed")
    _seed_completed_run(hub_env.repo_root, "newer-completed")
    save_state(
        hub_env.repo_root / ".codex-autorunner" / "state.sqlite3",
        RunnerState(
            last_run_id="older-failed",
            status="running",
            last_exit_code=137,
            last_run_started_at="2026-03-10T00:00:00+00:00",
            last_run_finished_at=None,
        ),
    )

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    repos = snapshot.get("repos") or []
    repo_entry = next(repo for repo in repos if repo.get("id") == hub_env.repo_id)
    assert repo_entry["last_run_id"] == "newer-completed"
    assert repo_entry["last_exit_code"] is None


def test_build_hub_snapshot_includes_pma_threads_section(hub_env) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    store = PmaThreadStore(hub_env.hub_root)
    thread = store.create_thread(
        "codex",
        workspace_root=hub_env.repo_root,
        repo_id=hub_env.repo_id,
        name="ad-hoc-refactor",
    )
    managed_thread_id = str(thread.get("managed_thread_id") or "")
    assert managed_thread_id
    store.update_thread_after_turn(
        managed_thread_id,
        last_turn_id="turn-001",
        last_message_preview=(
            "This is a long preview that should still be visible in rendered "
            "snapshot output for PMA managed threads."
        ),
    )

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    pma_threads = snapshot.get("pma_threads")
    assert isinstance(pma_threads, list)
    assert pma_threads
    first = pma_threads[0]
    assert first["managed_thread_id"] == managed_thread_id
    assert first["agent"] == "codex"
    assert first["repo_id"] == hub_env.repo_id
    assert first["resource_kind"] == "repo"
    assert first["resource_id"] == hub_env.repo_id
    assert first["status"] == "idle"
    assert first["lifecycle_status"] == "active"
    assert first["status_reason"] == "thread_created"
    assert "last_message_preview" in first
    thread_freshness = first.get("freshness") or {}
    assert thread_freshness.get("generated_at")
    assert thread_freshness.get("recency_basis") == "thread_status_changed_at"

    rendered = _render_hub_snapshot(snapshot)
    assert "Snapshot Freshness:" in rendered
    assert "PMA Managed Threads:" in rendered
    assert managed_thread_id in rendered
    assert f"repo_id={hub_env.repo_id}" in rendered
    assert "agent=codex" in rendered
    assert "status=idle" in rendered
    assert "last_turn=-" in rendered
    assert "reason=thread_created" in rendered
    assert "freshness: status=" in rendered


def test_build_hub_snapshot_includes_agent_workspaces_section(hub_env) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    supervisor = HubSupervisor.from_path(hub_env.hub_root)
    try:
        supervisor.create_agent_workspace(
            workspace_id="zc-main",
            runtime="zeroclaw",
            display_name="ZeroClaw Main",
            enabled=False,
        )
        snapshot = asyncio.run(
            build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
        )
    finally:
        supervisor.shutdown()

    agent_workspaces = snapshot.get("agent_workspaces")
    assert isinstance(agent_workspaces, list)
    assert agent_workspaces
    first = agent_workspaces[0]
    assert first["id"] == "zc-main"
    assert first["runtime"] == "zeroclaw"
    assert first["resource_kind"] == "agent_workspace"

    rendered = _render_hub_snapshot(snapshot)
    assert "Agent Workspaces:" in rendered
    assert "zc-main (ZeroClaw Main): runtime=zeroclaw" in rendered


def test_render_hub_snapshot_distinguishes_run_dispatch_vs_pma_files(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [
            {
                "item_type": "run_dispatch",
                "next_action": "reply_and_resume",
                "repo_id": "repo-1",
                "run_id": "run-1",
                "seq": 1,
                "dispatch": {
                    "mode": "pause",
                    "title": "Need input",
                    "body": "Please respond",
                    "is_handoff": False,
                },
                "files": ["request.md"],
                "open_url": "/repos/repo-1/?tab=inbox&run_id=run-1",
                "run_state": {
                    "state": "paused",
                    "blocking_reason": "Waiting for user input",
                },
            }
        ],
        "repos": [],
        "pma_files": {"inbox": ["upload.md", "data.csv"], "outbox": []},
        "pma_files_detail": {
            "inbox": [
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "upload.md",
                    "source": "filebox",
                    "size": "100",
                    "modified_at": "2024-01-01T00:00:00Z",
                },
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "data.csv",
                    "source": "filebox",
                    "size": "500",
                    "modified_at": "2024-01-01T00:01:00Z",
                },
            ],
            "outbox": [],
        },
    }

    result = _render_hub_snapshot(snapshot)

    assert "Run Dispatches (paused runs needing attention):" in result
    assert "next_action=reply_and_resume" in result
    assert "repo_id=repo-1" in result

    assert "PMA File Inbox:" in result
    assert "inbox: [upload.md, data.csv]" in result
    assert "next_action: process_uploaded_file" in result


def test_render_hub_snapshot_includes_repo_destination(tmp_path: Path) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [],
        "repos": [
            {
                "id": "repo-1",
                "display_name": "Repo One",
                "status": "idle",
                "last_run_id": None,
                "last_exit_code": None,
                "ticket_flow": None,
                "run_state": None,
                "effective_destination": {"kind": "docker", "image": "python:3.12"},
            }
        ],
        "pma_files": {"inbox": [], "outbox": []},
        "pma_files_detail": {"inbox": [], "outbox": []},
    }

    result = _render_hub_snapshot(snapshot)

    assert "Repos:" in result
    assert "destination=docker:python:3.12" in result


def test_render_hub_snapshot_pma_files_only(tmp_path: Path) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [],
        "repos": [],
        "pma_files": {"inbox": ["ticket-pack.md"], "outbox": []},
        "pma_files_detail": {
            "inbox": [
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "ticket-pack.md",
                    "source": "filebox",
                    "size": "200",
                    "modified_at": "2024-01-01T00:00:00Z",
                }
            ],
            "outbox": [],
        },
    }

    result = _render_hub_snapshot(snapshot)

    assert "Run Dispatches" not in result
    assert "PMA File Inbox:" in result
    assert "inbox: [ticket-pack.md]" in result
    assert "next_action: process_uploaded_file" in result


def test_render_hub_snapshot_marks_stale_pma_files_as_review_only(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [],
        "repos": [],
        "pma_files": {"inbox": ["forgotten.zip"], "outbox": []},
        "pma_files_detail": {
            "inbox": [
                {
                    "item_type": "pma_file",
                    "next_action": "review_stale_uploaded_file",
                    "box": "inbox",
                    "name": "forgotten.zip",
                    "source": "filebox",
                    "size": "200",
                    "modified_at": "2026-03-16T09:00:00Z",
                    "freshness": {
                        "generated_at": "2026-03-16T12:00:00Z",
                        "recency_basis": "file_modified_at",
                        "basis_at": "2026-03-16T09:00:00Z",
                        "is_stale": True,
                    },
                }
            ],
            "outbox": [],
        },
    }

    result = _render_hub_snapshot(snapshot)

    assert "PMA File Inbox:" in result
    assert "next_action: review_stale_uploaded_file" in result
    assert "likely_leftovers=[forgotten.zip]" in result
    assert "false positives from prior work" in result


def test_render_hub_snapshot_caps_pma_file_action_summaries(
    tmp_path: Path,
) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [],
        "repos": [],
        "pma_files": {
            "inbox": ["fresh-1.md", "stale-1.zip", "fresh-2.md"],
            "outbox": [],
        },
        "pma_files_detail": {
            "inbox": [
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "fresh-1.md",
                    "source": "filebox",
                    "size": "200",
                    "modified_at": "2026-03-29T09:00:00Z",
                },
                {
                    "item_type": "pma_file",
                    "next_action": "review_stale_uploaded_file",
                    "box": "inbox",
                    "name": "stale-1.zip",
                    "source": "filebox",
                    "size": "200",
                    "modified_at": "2026-03-16T09:00:00Z",
                },
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "fresh-2.md",
                    "source": "filebox",
                    "size": "200",
                    "modified_at": "2026-03-29T10:00:00Z",
                },
            ],
            "outbox": [],
        },
    }

    result = _render_hub_snapshot(snapshot, max_pma_files=2)

    assert "inbox: [fresh-1.md, stale-1.zip]" in result
    assert "fresh_uploads=[fresh-1.md]" in result
    assert "likely_leftovers=[stale-1.zip]" in result
    assert "fresh-2.md" not in result


def test_render_hub_snapshot_empty_both(tmp_path: Path) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [],
        "repos": [],
        "pma_files": {"inbox": [], "outbox": []},
        "pma_files_detail": {"inbox": [], "outbox": []},
    }

    result = _render_hub_snapshot(snapshot)

    assert "Run Dispatches" not in result
    assert "PMA File Inbox:" not in result
    assert "next_action: process_uploaded_file" not in result


def test_format_pma_prompt_includes_filebox_paths(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert ".codex-autorunner/filebox/outbox/" in result
    assert ".codex-autorunner/filebox/inbox/" in result


def test_render_hub_snapshot_includes_all_next_action_types(tmp_path: Path) -> None:
    from codex_autorunner.core.pma_context import _render_hub_snapshot

    seed_hub_files(tmp_path, force=True)

    snapshot = {
        "inbox": [
            {
                "item_type": "worker_dead",
                "next_action": "restart_worker",
                "repo_id": "repo-1",
                "run_id": "run-1",
                "seq": 1,
                "dispatch": None,
                "files": [],
                "open_url": "/repos/repo-1/?tab=inbox&run_id=run-1",
                "run_state": {
                    "state": "dead",
                    "blocking_reason": "Worker not running",
                },
            },
            {
                "item_type": "run_failed",
                "next_action": "diagnose_or_restart",
                "repo_id": "repo-2",
                "run_id": "run-2",
                "seq": 2,
                "dispatch": None,
                "files": [],
                "open_url": "/repos/repo-2/?tab=inbox&run_id=run-2",
                "run_state": {
                    "state": "blocked",
                    "blocking_reason": "Run failed",
                },
            },
        ],
        "repos": [],
        "pma_files": {"inbox": [], "outbox": []},
        "pma_files_detail": {"inbox": [], "outbox": []},
    }

    result = _render_hub_snapshot(snapshot)

    assert "Run Dispatches (paused runs needing attention):" in result
    assert "next_action=restart_worker" in result
    assert "next_action=diagnose_or_restart" in result
    assert "repo_id=repo-1" in result
    assert "repo_id=repo-2" in result
    assert "PMA File Inbox:" not in result


class TestIssue975DeltaPmaPromptAssembly:
    """Coverage for issue #975 delta-aware PMA prompt assembly."""

    def test_format_pma_prompt_structure_sections_in_order(
        self, tmp_path: Path
    ) -> None:
        """First turn keeps the full context path and action queue section ordering."""
        seed_hub_files(tmp_path, force=True)

        snapshot = {
            "inbox": [
                {
                    "item_type": "run_dispatch",
                    "next_action": "reply_and_resume",
                    "repo_id": "repo-char",
                    "run_id": "run-char-1",
                    "seq": 1,
                    "dispatch": {
                        "mode": "pause",
                        "title": "Baseline dispatch",
                        "body": "Please review.",
                        "is_handoff": False,
                    },
                    "files": ["review.md"],
                    "open_url": "/repos/repo-char/?tab=inbox&run_id=run-char-1",
                    "run_state": {
                        "state": "paused",
                        "blocking_reason": "Waiting for input",
                    },
                }
            ],
            "repos": [],
            "pma_files": {"inbox": [], "outbox": []},
            "pma_files_detail": {"inbox": [], "outbox": []},
        }

        result = format_pma_prompt(
            "Base prompt",
            snapshot,
            "User message",
            hub_root=tmp_path,
            prompt_state_key="pma.test-order",
        )

        preamble_idx = result.find("<pma_workspace_docs>")
        agents_idx = result.find("<AGENTS_MD>")
        active_context_idx = result.find("<ACTIVE_CONTEXT_MD>")
        context_log_idx = result.find("<CONTEXT_LOG_TAIL_MD>")
        fastpath_idx = result.find("<pma_fastpath>")
        change_idx = result.find("<what_changed_since_last_turn")
        actionable_idx = result.find("<current_actionable_state>\n")
        snapshot_idx = result.find("<hub_snapshot>\n")
        user_msg_idx = result.find("<user_message>\n")

        assert preamble_idx >= 0
        assert agents_idx > preamble_idx
        assert active_context_idx > agents_idx
        assert context_log_idx > active_context_idx
        assert fastpath_idx > context_log_idx
        assert change_idx > fastpath_idx
        assert actionable_idx > change_idx
        assert snapshot_idx > actionable_idx
        assert user_msg_idx > snapshot_idx

    def test_format_pma_prompt_reuses_durable_context_by_digest_after_first_turn(
        self, tmp_path: Path
    ) -> None:
        """Repeated turns reference unchanged docs and full snapshot by digest."""
        seed_hub_files(tmp_path, force=True)

        agents_path = tmp_path / ".codex-autorunner" / "pma" / "docs" / "AGENTS.md"
        active_context_path = (
            tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
        )
        agents_path.write_text(
            "# Durable Agent Guidance\n\nRule 1: Be concise.\n", encoding="utf-8"
        )
        active_context_path.write_text(
            "# Current Context\n\nWorking on issue 975.\n", encoding="utf-8"
        )

        snapshot = {
            "generated_at": "2026-03-16T00:00:00Z",
            "action_queue": [
                {
                    "action_queue_id": "ticket_flow_inbox:repo-1:run-1",
                    "queue_source": "ticket_flow_inbox",
                    "queue_rank": 1,
                    "item_type": "run_dispatch",
                    "repo_id": "repo-1",
                    "run_id": "run-1",
                    "recommended_action": "reply_and_resume",
                    "precedence": {"rank": 10, "label": "ticket_flow_inbox"},
                    "supersession": {"status": "primary", "is_primary": True},
                }
            ],
            "inbox": [],
            "repos": [],
            "pma_files": {"inbox": [], "outbox": []},
        }
        result1 = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-delta",
        )
        result2 = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-delta",
        )

        assert "# Durable Agent Guidance" in result1
        assert "# Current Context" in result1
        assert "<pma_fastpath>" in result1
        assert "<pma_workspace_docs>" not in result2
        assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in result2
        assert "<pma_fastpath>" not in result2
        assert "\n<hub_snapshot>\n" not in result2
        assert "<hub_snapshot_ref " in result2
        assert "- cached=" in result2
        assert "PMA_PROMPT_MD" in result2
        assert "PMA_DISCOVERABILITY" in result2
        assert "PMA_FASTPATH" in result2
        assert "AGENTS_MD" in result2
        assert "ACTIVE_CONTEXT_MD" in result2
        assert "CONTEXT_LOG_TAIL_MD" in result2
        assert "HUB_SNAPSHOT" in result2
        assert "- changed=none" in result2
        assert "PMA Action Queue:" in result2
        assert "recommended_action=reply_and_resume" in result2

    def test_format_pma_prompt_delta_surfaces_only_changed_docs(
        self, tmp_path: Path
    ) -> None:
        """Changed durable docs are re-injected while unchanged docs stay digest-only."""
        seed_hub_files(tmp_path, force=True)

        agents_path = tmp_path / ".codex-autorunner" / "pma" / "docs" / "AGENTS.md"
        active_context_path = (
            tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
        )
        agents_path.write_text(
            "# Durable Agent Guidance\n\nRule 1: Be concise.\n", encoding="utf-8"
        )
        active_context_path.write_text(
            "# Current Context\n\nWorking on issue 975.\n", encoding="utf-8"
        )

        snapshot = {
            "generated_at": "2026-03-16T00:00:00Z",
            "action_queue": [],
            "inbox": [],
            "repos": [],
            "pma_files": {"inbox": [], "outbox": []},
            "pma_files_detail": {"inbox": [], "outbox": []},
        }

        _ = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-changed-docs",
        )
        active_context_path.write_text(
            "# Current Context\n\nWorking on issue 975.\n\nNext: wire delta prompt.\n",
            encoding="utf-8",
        )
        result = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-changed-docs",
        )

        assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in result
        assert "<pma_fastpath>" not in result
        assert "- cached=" in result
        assert "AGENTS_MD" in result
        assert "- changed=ACTIVE_CONTEXT_MD" in result
        assert "<ACTIVE_CONTEXT_MD>" in result
        assert "Next: wire delta prompt." in result
        assert "Rule 1: Be concise." not in result
        assert "\n<hub_snapshot>\n" not in result

    def test_format_pma_prompt_delta_surfaces_changed_hub_snapshot_without_repeating_stable_blocks(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)

        snapshot1 = {
            "generated_at": "2026-03-16T00:00:00Z",
            "action_queue": [],
            "inbox": [],
            "repos": [],
            "pma_files": {"inbox": [], "outbox": []},
            "pma_files_detail": {"inbox": [], "outbox": []},
        }
        snapshot2 = {
            "generated_at": "2026-03-16T00:01:00Z",
            "action_queue": [
                {
                    "action_queue_id": "managed_thread_followup:thread-1",
                    "queue_source": "managed_thread_followup",
                    "queue_rank": 1,
                    "item_type": "managed_thread",
                    "managed_thread_id": "thread-1",
                    "recommended_action": "inspect_thread_result",
                    "precedence": {"rank": 20, "label": "managed_thread_followup"},
                    "supersession": {"status": "primary", "is_primary": True},
                }
            ],
            "inbox": [],
            "repos": [],
            "pma_files": {"inbox": [], "outbox": []},
            "pma_files_detail": {"inbox": [], "outbox": []},
        }

        _ = format_pma_prompt(
            "Base prompt",
            snapshot1,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-changed-hub-snapshot",
        )
        result = format_pma_prompt(
            "Base prompt",
            snapshot2,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-changed-hub-snapshot",
        )

        assert "Ops guide: `.codex-autorunner/pma/docs/ABOUT_CAR.md`." not in result
        assert "<pma_workspace_docs>" not in result
        assert "<pma_fastpath>" not in result
        assert "- changed=HUB_SNAPSHOT (see <current_actionable_state>)" in result
        assert "\n<hub_snapshot>\n" not in result
        assert "<hub_snapshot_ref " in result
        assert "PMA Action Queue:" in result
        assert "recommended_action=inspect_thread_result" in result

    def test_format_pma_prompt_force_full_context_refresh(self, tmp_path: Path) -> None:
        seed_hub_files(tmp_path, force=True)

        snapshot = {"inbox": [], "repos": [], "pma_files": {"inbox": [], "outbox": []}}
        _ = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-refresh",
        )
        result = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-refresh",
            force_full_context=True,
        )

        assert "<pma_workspace_docs>" in result
        assert "<hub_snapshot>" in result
        assert "reason='explicit_refresh'" in result

    def test_format_pma_prompt_digest_mismatch_falls_back_to_full_context(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)

        snapshot = {"inbox": [], "repos": [], "pma_files": {"inbox": [], "outbox": []}}
        _ = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-digest-mismatch",
        )

        state_path = tmp_path / ".codex-autorunner" / "pma" / "prompt_state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["sessions"]["pma.test-digest-mismatch"][
            "bundle_digest"
        ] = "not-a-digest"
        state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        result = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-digest-mismatch",
        )

        assert "<pma_workspace_docs>" in result
        assert "<hub_snapshot>" in result
        assert "reason='digest_mismatch'" in result

    def test_format_pma_prompt_without_state_key_always_full_context(
        self, tmp_path: Path
    ) -> None:
        """Without prompt_state_key, every turn sends full context (no delta mode)."""
        seed_hub_files(tmp_path, force=True)

        snapshot = {"inbox": [], "repos": [], "pma_files": {"inbox": [], "outbox": []}}

        result1 = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
        )
        result2 = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
        )

        assert "<pma_workspace_docs>" in result1
        assert "<pma_workspace_docs>" in result2
        assert "<hub_snapshot>" in result1
        assert "<hub_snapshot>" in result2
        assert "<what_changed_since_last_turn" not in result1
        assert "<what_changed_since_last_turn" not in result2

    def test_format_pma_prompt_delta_header_includes_all_sections(
        self, tmp_path: Path
    ) -> None:
        """Delta header summarizes all cached sections compactly."""
        seed_hub_files(tmp_path, force=True)

        snapshot = {"inbox": [], "repos": [], "pma_files": {"inbox": [], "outbox": []}}
        _ = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 1",
            hub_root=tmp_path,
            prompt_state_key="pma.test-header-sections",
        )
        result = format_pma_prompt(
            "Base prompt",
            snapshot,
            "Turn 2",
            hub_root=tmp_path,
            prompt_state_key="pma.test-header-sections",
        )

        assert "- cached=" in result
        assert "PMA_PROMPT_MD" in result
        assert "PMA_DISCOVERABILITY" in result
        assert "PMA_FASTPATH" in result
        assert "AGENTS_MD" in result
        assert "ACTIVE_CONTEXT_MD" in result
        assert "CONTEXT_LOG_TAIL_MD" in result
        assert "HUB_SNAPSHOT" in result
        assert "- changed=none" in result
        assert "state_key='pma.test-header-sections'" in result

    def test_format_pma_prompt_compacted_context_stays_within_budget(
        self, tmp_path: Path
    ) -> None:
        """Auto-pruned active_context stays within budget across multiple prunes."""
        seed_hub_files(tmp_path, force=True)

        active_context_path = (
            tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
        )
        context_log_path = (
            tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
        )

        _write_hub_config(
            tmp_path,
            {
                "mode": "hub",
                "pma": {
                    "docs_max_chars": 12000,
                    "active_context_max_lines": 20,
                    "context_log_tail_lines": 50,
                },
            },
        )

        snapshot = {"inbox": [], "repos": [], "pma_files": {"inbox": [], "outbox": []}}

        for turn_num in range(3):
            long_content = "\n".join(f"Turn {turn_num} line {idx}" for idx in range(30))
            active_context_path.write_text(long_content, encoding="utf-8")

            format_pma_prompt(
                "Base prompt",
                snapshot,
                f"Turn {turn_num}",
                hub_root=tmp_path,
                prompt_state_key=f"pma.test-multi-prune-{turn_num}",
            )

            pruned_content = active_context_path.read_text(encoding="utf-8")
            line_count = len(pruned_content.splitlines())
            assert (
                line_count <= 25
            ), f"Turn {turn_num}: active_context exceeded budget ({line_count} lines)"

        log_content = context_log_path.read_text(encoding="utf-8")
        assert "## Snapshot:" in log_content
        assert "Turn 0 line" in log_content
        assert "Turn 1 line" in log_content
        assert "Turn 2 line" in log_content


class TestIssue975CharacterizationMixedPmaState:
    """Characterization tests for mixed PMA state snapshots (issue #975).

    These tests document the baseline for a hub snapshot containing:
    - Mixed run inbox items (paused, failed, completed)
    - Managed threads with various statuses
    - PMA file inbox entries
    - Freshness metadata

    The fixture helper can be reused by later tickets to verify changes.
    """

    @staticmethod
    def build_mixed_pma_snapshot(
        *,
        include_dispatch: bool = True,
        include_failed_run: bool = True,
        include_completed_run: bool = True,
        include_pma_thread: bool = True,
        include_pma_file: bool = True,
    ) -> dict:
        """Build a mixed PMA snapshot for characterization tests.

        This helper creates a representative snapshot with all the state
        categories that issue #975 touches. Later tickets can use this
        to verify action-queue, status-label, and prompt-delta changes.
        """
        inbox: list[dict] = []

        if include_dispatch:
            inbox.append(
                {
                    "item_type": "run_dispatch",
                    "next_action": "reply_and_resume",
                    "repo_id": "repo-mixed",
                    "run_id": "run-dispatch-1",
                    "seq": 3,
                    "dispatch": {
                        "mode": "pause",
                        "title": "Mixed state dispatch",
                        "body": "Please review this mixed state.",
                        "is_handoff": False,
                    },
                    "files": ["request.md", "context.json"],
                    "open_url": "/repos/repo-mixed/?tab=inbox&run_id=run-dispatch-1",
                    "run_state": {
                        "state": "paused",
                        "blocking_reason": "Waiting for operator input",
                        "recommended_action": "car flow ticket_flow resume",
                        "recommended_actions": [
                            "car flow ticket_flow resume",
                            "car flow ticket_flow status",
                        ],
                        "attention_required": True,
                    },
                    "canonical_state_v1": {
                        "schema_version": 1,
                        "repo_id": "repo-mixed",
                        "latest_run_id": "run-dispatch-1",
                        "latest_run_status": "paused",
                        "state": "paused",
                        "recommended_action": "car flow ticket_flow resume",
                        "recommendation_confidence": "high",
                        "freshness": {
                            "generated_at": "2026-03-16T12:00:00Z",
                            "recency_basis": "run_state",
                            "basis_at": "2026-03-16T11:55:00Z",
                            "is_stale": False,
                        },
                    },
                }
            )

        if include_failed_run:
            inbox.append(
                {
                    "item_type": "run_failed",
                    "next_action": "diagnose_or_restart",
                    "repo_id": "repo-mixed",
                    "run_id": "run-failed-1",
                    "seq": None,
                    "dispatch": None,
                    "files": [],
                    "open_url": "/repos/repo-mixed/?tab=inbox&run_id=run-failed-1",
                    "run_state": {
                        "state": "blocked",
                        "blocking_reason": "Run failed: connection timeout",
                        "recommended_action": "car flow ticket_flow resume --force",
                        "attention_required": False,
                    },
                    "canonical_state_v1": {
                        "schema_version": 1,
                        "repo_id": "repo-mixed",
                        "latest_run_id": "run-failed-1",
                        "latest_run_status": "failed",
                        "state": "blocked",
                        "recommendation_confidence": "medium",
                        "freshness": {
                            "generated_at": "2026-03-16T11:00:00Z",
                            "recency_basis": "run_state",
                            "basis_at": "2026-03-16T10:30:00Z",
                            "is_stale": True,
                        },
                    },
                }
            )

        if include_completed_run:
            inbox.append(
                {
                    "item_type": "run_completed",
                    "next_action": "start_new_flow",
                    "repo_id": "repo-mixed",
                    "run_id": "run-completed-1",
                    "seq": None,
                    "dispatch": None,
                    "files": [],
                    "open_url": "/repos/repo-mixed/?tab=inbox&run_id=run-completed-1",
                    "run_state": {
                        "state": "completed",
                        "blocking_reason": None,
                        "recommended_action": "car flow ticket_flow start",
                        "attention_required": False,
                    },
                    "canonical_state_v1": {
                        "schema_version": 1,
                        "repo_id": "repo-mixed",
                        "latest_run_id": "run-completed-1",
                        "latest_run_status": "completed",
                        "state": "completed",
                        "recommendation_confidence": "low",
                        "recommendation_stale_reason": "run already completed",
                        "freshness": {
                            "generated_at": "2026-03-16T09:00:00Z",
                            "recency_basis": "run_state",
                            "basis_at": "2026-03-16T08:00:00Z",
                            "is_stale": True,
                        },
                    },
                }
            )

        pma_threads: list[dict] = []
        if include_pma_thread:
            pma_threads.append(
                {
                    "managed_thread_id": "thread-idle-1",
                    "agent": "codex",
                    "repo_id": "repo-mixed",
                    "resource_kind": "repo",
                    "resource_id": "repo-mixed",
                    "workspace_root": "/worktrees/repo-mixed",
                    "name": "refactor-thread",
                    "status": "idle",
                    "lifecycle_status": "active",
                    "status_reason": "managed_turn_completed",
                    "status_terminal": False,
                    "status_changed_at": "2026-03-16T11:00:00Z",
                    "last_turn_id": "turn-001",
                    "last_message_preview": "Refactoring complete, ready for review.",
                    "updated_at": "2026-03-16T11:30:00Z",
                    "freshness": {
                        "generated_at": "2026-03-16T12:00:00Z",
                        "recency_basis": "thread_status_changed_at",
                        "basis_at": "2026-03-16T11:00:00Z",
                        "is_stale": False,
                    },
                }
            )
            pma_threads.append(
                {
                    "managed_thread_id": "thread-completed-1",
                    "agent": "opencode",
                    "repo_id": "repo-mixed",
                    "resource_kind": "repo",
                    "resource_id": "repo-mixed",
                    "workspace_root": "/worktrees/repo-mixed",
                    "name": "completed-thread",
                    "status": "completed",
                    "lifecycle_status": "active",
                    "status_reason": "managed_turn_completed",
                    "status_terminal": False,
                    "status_changed_at": "2026-03-16T09:00:00Z",
                    "last_turn_id": "turn-002",
                    "last_message_preview": "Task finished successfully.",
                    "updated_at": "2026-03-16T11:45:00Z",
                    "freshness": {
                        "generated_at": "2026-03-16T12:00:00Z",
                        "recency_basis": "thread_status_changed_at",
                        "basis_at": "2026-03-16T09:00:00Z",
                        "is_stale": True,
                    },
                }
            )

        pma_files_detail: dict[str, list[dict]] = {"inbox": [], "outbox": []}
        if include_pma_file:
            pma_files_detail["inbox"] = [
                {
                    "item_type": "pma_file",
                    "next_action": "process_uploaded_file",
                    "box": "inbox",
                    "name": "ticket-pack.md",
                    "source": "filebox",
                    "size": "1500",
                    "modified_at": "2026-03-16T11:30:00Z",
                    "freshness": {
                        "generated_at": "2026-03-16T12:00:00Z",
                        "recency_basis": "file_modified_at",
                        "basis_at": "2026-03-16T11:30:00Z",
                        "is_stale": False,
                    },
                }
            ]

        from codex_autorunner.core.pma_context import build_pma_action_queue

        action_queue = build_pma_action_queue(
            inbox=inbox,
            pma_threads=pma_threads,
            pma_files_detail=pma_files_detail,
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )

        return {
            "generated_at": "2026-03-16T12:00:00Z",
            "inbox": inbox,
            "action_queue": action_queue,
            "repos": [],
            "pma_threads": pma_threads,
            "pma_files": {"inbox": ["ticket-pack.md"], "outbox": []},
            "pma_files_detail": pma_files_detail,
            "freshness": {
                "generated_at": "2026-03-16T12:00:00Z",
                "stale_threshold_seconds": 3600,
                "sections": {
                    "inbox": {
                        "entity_count": len(inbox),
                        "fresh_count": sum(
                            1
                            for item in inbox
                            if not (item.get("canonical_state_v1") or {})
                            .get("freshness", {})
                            .get("is_stale", False)
                        ),
                        "stale_count": sum(
                            1
                            for item in inbox
                            if (item.get("canonical_state_v1") or {})
                            .get("freshness", {})
                            .get("is_stale", False)
                        ),
                    },
                    "action_queue": {
                        "entity_count": len(action_queue),
                        "stale_count": sum(
                            1
                            for item in action_queue
                            if (item.get("freshness") or {}).get("is_stale", False)
                        ),
                    },
                    "pma_threads": {"entity_count": len(pma_threads), "stale_count": 0},
                },
            },
        }

    def test_mixed_snapshot_rendered_contains_all_categories(
        self, tmp_path: Path
    ) -> None:
        """Verify mixed snapshot renders all categories in hub_snapshot."""
        from codex_autorunner.core.pma_context import _render_hub_snapshot

        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()

        result = _render_hub_snapshot(snapshot)

        assert "Run Dispatches (paused runs needing attention):" in result
        assert "repo_id=repo-mixed" in result
        assert "run_id=run-dispatch-1" in result
        assert "next_action=reply_and_resume" in result
        assert "next_action=diagnose_or_restart" in result

        assert "PMA Managed Threads:" in result
        assert "thread-idle-1" in result
        assert "thread-completed-1" in result
        assert "status=idle" in result
        assert "status=reusable" in result
        assert "last_turn=completed" in result

        assert "PMA File Inbox:" in result
        assert "ticket-pack.md" in result

    def test_mixed_snapshot_freshness_metadata_present(self, tmp_path: Path) -> None:
        """Verify freshness metadata is attached to items in mixed snapshot."""
        from codex_autorunner.core.pma_context import _render_hub_snapshot

        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()

        result = _render_hub_snapshot(snapshot)

        assert "Snapshot Freshness:" in result
        assert "stale_threshold_seconds=" in result
        assert "section=inbox" in result

    def test_format_pma_prompt_with_mixed_snapshot(self, tmp_path: Path) -> None:
        """Verify format_pma_prompt handles mixed snapshot correctly."""
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()

        result = format_pma_prompt(
            "Base prompt", snapshot, "Mixed state user message", hub_root=tmp_path
        )

        assert "<hub_snapshot>" in result
        assert "</hub_snapshot>" in result
        assert "<user_message>" in result
        assert "Mixed state user message" in result

        assert "Run Dispatches (paused runs needing attention):" in result
        assert "PMA Managed Threads:" in result
        assert "PMA File Inbox:" in result

    def test_mixed_snapshot_action_queue_marks_primary_and_superseded_items(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()

        queue = snapshot.get("action_queue") or []
        assert queue

        primary = queue[0]
        assert primary["queue_source"] == "ticket_flow_inbox"
        assert primary["item_type"] == "run_dispatch"
        assert primary["recommended_action"] == "reply_and_resume"
        assert primary["supersession"]["status"] == "primary"
        assert primary["supersession"]["is_primary"] is True

        failed_run = next(
            item for item in queue if item.get("run_id") == "run-failed-1"
        )
        assert failed_run["supersession"]["status"] == "superseded"
        assert failed_run["supersession"]["superseded_by"] == primary["action_queue_id"]

        reusable_summary = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "reusable"
        )
        assert reusable_summary["queue_source"] == "managed_thread_followup"
        assert reusable_summary["thread_count"] == 1
        assert reusable_summary["followup_state"] == "reusable"
        assert reusable_summary["followup_state_counts"] == {"reusable": 1}
        assert reusable_summary["recommended_action"] == "show_reusable_threads"
        assert reusable_summary["supersession"]["status"] == "superseded"
        assert (
            reusable_summary["supersession"]["superseded_by"]
            == primary["action_queue_id"]
        )

        cleanup_summary = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "idle_archive_candidate"
        )
        assert cleanup_summary["queue_source"] == "managed_thread_followup"
        assert cleanup_summary["thread_count"] == 1
        assert cleanup_summary["followup_state"] == "idle_archive_candidate"
        assert cleanup_summary["followup_state_counts"] == {"idle_archive_candidate": 1}
        assert cleanup_summary["recommended_action"] == "show_cleanup_candidates"
        assert cleanup_summary["supersession"]["status"] == "superseded"
        assert (
            cleanup_summary["supersession"]["superseded_by"]
            == primary["action_queue_id"]
        )

        file_item = next(item for item in queue if item.get("item_type") == "pma_file")
        assert file_item["queue_source"] == "pma_file_inbox"
        assert file_item["supersession"]["status"] == "non_primary"

    def test_mixed_snapshot_rendered_includes_action_queue_section(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.pma_context import _render_hub_snapshot

        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()

        result = _render_hub_snapshot(snapshot)

        assert "PMA Action Queue:" in result
        assert "source=ticket_flow_inbox" in result
        assert "status=primary" in result
        assert "status=superseded" in result
        assert "item_type=managed_thread_followup_summary" in result
        assert "name=Reusable managed threads (1)" in result
        assert "followup_state=reusable" in result
        assert "show reusable threads" in result

    def test_snapshot_pma_threads_expose_chat_binding_metadata(self, hub_env) -> None:
        from codex_autorunner.core.pma_context import _snapshot_pma_threads

        store = PmaThreadStore(hub_env.hub_root)
        thread = store.create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="chat-bound-thread",
        )
        thread_id = str(thread["managed_thread_id"])
        OrchestrationBindingStore(hub_env.hub_root).upsert_binding(
            surface_kind="discord",
            surface_key="discord:channel-123",
            thread_target_id=thread_id,
            agent_id="codex",
            repo_id=hub_env.repo_id,
            mode="reuse",
        )

        snapshot_threads = _snapshot_pma_threads(hub_env.hub_root)
        thread_snapshot = next(
            item
            for item in snapshot_threads
            if item.get("managed_thread_id") == thread_id
        )

        assert thread_snapshot["chat_bound"] is True
        assert thread_snapshot["binding_kind"] == "discord"
        assert thread_snapshot["binding_id"] == "discord:channel-123"
        assert thread_snapshot["binding_count"] == 1
        assert thread_snapshot["binding_kinds"] == ["discord"]
        assert thread_snapshot["binding_ids"] == ["discord:channel-123"]
        assert thread_snapshot["cleanup_protected"] is True

    def test_snapshot_pma_threads_keeps_inventory_when_binding_lookup_fails(
        self, hub_env
    ) -> None:
        from codex_autorunner.core.pma_context import _snapshot_pma_threads

        store = PmaThreadStore(hub_env.hub_root)
        thread = store.create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="binding-failure-thread",
        )
        thread_id = str(thread["managed_thread_id"])

        with patch(
            "codex_autorunner.core.pma_context.active_chat_binding_metadata_by_thread",
            side_effect=RuntimeError("binding db unavailable"),
        ):
            snapshot_threads = _snapshot_pma_threads(hub_env.hub_root)

        thread_snapshot = next(
            item
            for item in snapshot_threads
            if item.get("managed_thread_id") == thread_id
        )
        assert thread_snapshot["chat_bound"] is False
        assert thread_snapshot["binding_kind"] is None
        assert thread_snapshot["binding_count"] == 0
        assert thread_snapshot["cleanup_protected"] is False

    def test_completed_thread_queue_item_is_optional_reuse_not_immediate_followup(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_dispatch=False,
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )
        pma_threads = snapshot.get("pma_threads") or []
        completed_thread = next(
            item
            for item in pma_threads
            if item.get("managed_thread_id") == "thread-completed-1"
        )
        completed_thread["repo_id"] = "repo-other"
        completed_thread["resource_id"] = "repo-other"

        from codex_autorunner.core.pma_context import build_pma_action_queue

        queue = build_pma_action_queue(
            inbox=[],
            pma_threads=pma_threads,
            pma_files_detail={"inbox": [], "outbox": []},
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )
        reusable_item = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "reusable"
        )

        assert reusable_item["followup_state"] == "reusable"
        assert reusable_item["operator_need"] == "optional"
        assert reusable_item["thread_count"] == 1
        assert reusable_item["recommended_action"] == "show_reusable_threads"
        assert "counts-first summary" in (reusable_item.get("why_selected") or "")
        assert "thread-idle-1" in (reusable_item.get("managed_thread_ids") or [])

    def test_low_signal_reusable_summary_does_not_inherit_repo_id_from_mixed_owners(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )
        pma_threads = snapshot.get("pma_threads") or []
        pma_threads.append(
            {
                "managed_thread_id": "thread-unowned-1",
                "agent": "codex",
                "repo_id": "",
                "resource_kind": "",
                "resource_id": "",
                "workspace_root": "",
                "name": "unowned-thread",
                "status": "idle",
                "lifecycle_status": "active",
                "status_reason": "managed_turn_completed",
                "status_terminal": False,
                "status_changed_at": "2026-03-16T11:10:00Z",
                "last_turn_id": "turn-003",
                "last_message_preview": "Unowned reusable thread.",
                "updated_at": "2026-03-16T11:40:00Z",
                "freshness": {
                    "generated_at": "2026-03-16T12:00:00Z",
                    "recency_basis": "thread_status_changed_at",
                    "basis_at": "2026-03-16T11:10:00Z",
                    "is_stale": False,
                },
            }
        )

        from codex_autorunner.core.pma_context import build_pma_action_queue

        queue = build_pma_action_queue(
            inbox=snapshot.get("inbox") or [],
            pma_threads=pma_threads,
            pma_files_detail={"inbox": [], "outbox": []},
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )
        reusable_item = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "reusable"
        )

        assert reusable_item["repo_id"] is None
        assert reusable_item["supersession"]["status"] == "non_primary"
        assert "thread-idle-1" in (reusable_item.get("managed_thread_ids") or [])
        assert "thread-unowned-1" in (reusable_item.get("managed_thread_ids") or [])

    def test_recently_resumed_thread_with_history_stays_reusable_inventory(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_dispatch=False,
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )
        pma_threads = snapshot.get("pma_threads") or []
        resumed_thread = next(
            item
            for item in pma_threads
            if item.get("managed_thread_id") == "thread-idle-1"
        )
        resumed_thread["status_reason"] = "thread_resumed"
        resumed_thread["repo_id"] = "repo-resumed"
        resumed_thread["resource_id"] = "repo-resumed"

        from codex_autorunner.core.pma_context import build_pma_action_queue

        queue = build_pma_action_queue(
            inbox=[],
            pma_threads=pma_threads,
            pma_files_detail={"inbox": [], "outbox": []},
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )
        resumed_item = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "reusable"
            and "thread-idle-1" in (item.get("managed_thread_ids") or [])
        )

        assert resumed_item["followup_state"] == "reusable"
        assert resumed_item["operator_need"] == "optional"
        assert resumed_item["recommended_action"] == "show_reusable_threads"
        assert "thread-idle-1" in (resumed_item.get("managed_thread_ids") or [])

    def test_low_signal_inventory_queue_has_no_primary_next_action(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.pma_context import _render_hub_snapshot

        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_dispatch=False,
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )

        queue = snapshot.get("action_queue") or []
        assert queue
        assert all(
            (item.get("supersession") or {}).get("status") != "primary"
            for item in queue
        )

        rendered = _render_hub_snapshot(snapshot)
        assert "No strong next-action item right now" in rendered

    def test_stale_completed_thread_queue_item_becomes_cleanup_candidate(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_dispatch=False,
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )
        pma_threads = snapshot.get("pma_threads") or []
        stale_thread = next(
            item
            for item in pma_threads
            if item.get("managed_thread_id") == "thread-completed-1"
        )
        stale_thread["repo_id"] = "repo-other"
        stale_thread["resource_id"] = "repo-other"

        from codex_autorunner.core.pma_context import build_pma_action_queue

        queue = build_pma_action_queue(
            inbox=[],
            pma_threads=pma_threads,
            pma_files_detail={"inbox": [], "outbox": []},
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )

        archive_item = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "idle_archive_candidate"
        )
        assert archive_item["followup_state"] == "idle_archive_candidate"
        assert archive_item["operator_need"] == "cleanup"
        assert archive_item["thread_count"] == 1
        assert archive_item["recommended_action"] == "show_cleanup_candidates"
        assert "counts-first summary" in (archive_item.get("why_selected") or "")
        assert "thread-completed-1" in (archive_item.get("managed_thread_ids") or [])

    def test_stale_chat_bound_thread_becomes_protected_inventory(
        self, tmp_path: Path
    ) -> None:
        from codex_autorunner.core.pma_context import (
            _render_hub_snapshot,
            build_pma_action_queue,
        )

        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot(
            include_dispatch=False,
            include_failed_run=False,
            include_completed_run=False,
            include_pma_file=False,
        )
        pma_threads = snapshot.get("pma_threads") or []
        stale_thread = next(
            item
            for item in pma_threads
            if item.get("managed_thread_id") == "thread-completed-1"
        )
        stale_thread["chat_bound"] = True
        stale_thread["binding_kind"] = "discord"
        stale_thread["binding_id"] = "discord:channel-123"
        stale_thread["binding_count"] = 1
        stale_thread["binding_kinds"] = ["discord"]
        stale_thread["binding_ids"] = ["discord:channel-123"]
        stale_thread["cleanup_protected"] = True

        queue = build_pma_action_queue(
            inbox=[],
            pma_threads=pma_threads,
            pma_files_detail={"inbox": [], "outbox": []},
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )

        protected_item = next(
            item
            for item in queue
            if item.get("item_type") == "managed_thread_followup_summary"
            and item.get("followup_state") == "protected_chat_bound"
        )
        assert protected_item["operator_need"] == "protected"
        assert protected_item["thread_count"] == 1
        assert protected_item["recommended_action"] == "show_protected_threads"
        assert protected_item["cleanup_protected"] is True
        assert protected_item["chat_bound_thread_count"] == 1
        assert "thread-completed-1" in (protected_item.get("managed_thread_ids") or [])
        assert not any(
            "thread-completed-1" in (item.get("managed_thread_ids") or [])
            for item in queue
            if item.get("followup_state") == "idle_archive_candidate"
        )

        snapshot["action_queue"] = queue
        rendered = _render_hub_snapshot(snapshot)
        assert "Protected chat-bound threads (1)" in rendered
        assert "clean up workspace" in rendered

    def test_stale_pma_file_queue_item_is_marked_as_review_not_process(
        self, tmp_path: Path
    ) -> None:
        seed_hub_files(tmp_path, force=True)
        snapshot = self.build_mixed_pma_snapshot()
        pma_files_detail = snapshot.get("pma_files_detail") or {}
        stale_file = (pma_files_detail.get("inbox") or [])[0]
        stale_file["freshness"] = {
            "generated_at": "2026-03-16T12:00:00Z",
            "recency_basis": "file_modified_at",
            "basis_at": "2026-03-16T09:00:00Z",
            "is_stale": True,
        }

        from codex_autorunner.core.pma_context import build_pma_action_queue

        queue = build_pma_action_queue(
            inbox=snapshot.get("inbox") or [],
            pma_threads=snapshot.get("pma_threads") or [],
            pma_files_detail=pma_files_detail,
            automation={},
            generated_at="2026-03-16T12:00:00Z",
            stale_threshold_seconds=3600,
        )

        file_item = next(
            item for item in queue if item.get("item_type") == "pma_file_summary"
        )
        assert file_item["recommended_action"] == "show_stale_uploaded_files"
        assert file_item["next_action"] == "review_stale_uploaded_file"
        assert file_item["file_count"] == 1
        assert file_item["likely_false_positive"] is True
        assert "counts-first summary" in (file_item["why_selected"] or "")


class TestIssue975CharacterizationManagedThreadPayload:
    """Characterization tests for managed-thread operator payload shape (issue #975).

    These tests document the operator-facing status model:
    - status field shows operator-facing state (idle, running, paused, reusable, attention_required, archived)
    - lifecycle_status field contains machine-level lifecycle (active, archived)
    - last_turn field shows the raw turn outcome (completed, failed, -)
    - Reusable threads (completed + active) show as status=reusable with last_turn=completed

    The operator-facing status derives from derive_managed_thread_operator_status:
    - completed + active -> reusable
    - failed + active -> attention_required
    - idle/running/paused/archived -> same
    """

    def test_pma_thread_payload_includes_all_status_fields(self, hub_env) -> None:
        """Document that managed thread payloads include all status fields.

        Note: The 'status' field contains lifecycle_status (e.g., 'active'),
        while 'normalized_status' contains the runtime status (e.g., 'idle').
        This baseline documents the status model where:
        - status == lifecycle_status (machine-level lifecycle)
        - normalized_status == runtime status (operator-facing runtime state)
        """
        store = PmaThreadStore(hub_env.hub_root)
        thread = store.create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="status-characterization",
        )

        assert "managed_thread_id" in thread
        assert thread["status"] == "active"
        assert thread["lifecycle_status"] == "active"
        assert thread["normalized_status"] == "idle"
        assert thread["status_reason"] == "thread_created"
        assert thread["status_terminal"] is False

    def test_completed_thread_shows_completed_normalized_status_with_active_lifecycle(
        self, hub_env
    ) -> None:
        """Document the completed+active thread state.

        After a turn completes, normalized_status becomes 'completed' while
        lifecycle_status remains 'active'. The rendered output shows this as
        'status=reusable last_turn=completed' to indicate the thread is ready
        for reuse.

        Note: status_terminal is True for 'completed' status at the storage level,
        but the operator-facing status is 'reusable' which indicates the thread
        can accept another turn.
        """
        store = PmaThreadStore(hub_env.hub_root)
        thread = store.create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="completed-baseline",
        )
        thread_id = thread["managed_thread_id"]

        store.create_turn(thread_id, prompt="First turn")
        store.mark_turn_finished(
            store.get_running_turn(thread_id)["managed_turn_id"],
            status="ok",
            assistant_text="Done",
        )

        updated = store.get_thread(thread_id)
        assert updated["status"] == "active"
        assert updated["lifecycle_status"] == "active"
        assert updated["normalized_status"] == "completed"
        assert updated["status_terminal"] is True

    def test_hub_snapshot_renders_reusable_status_for_completed_threads(
        self, hub_env
    ) -> None:
        """Document that completed+active threads render as 'status=reusable last_turn=completed'."""
        from codex_autorunner.core.pma_context import _render_hub_snapshot

        store = PmaThreadStore(hub_env.hub_root)
        thread = store.create_thread(
            "codex",
            hub_env.repo_root,
            repo_id=hub_env.repo_id,
            name="snapshot-render-test",
        )
        thread_id = thread["managed_thread_id"]

        store.create_turn(thread_id, prompt="Turn for render test")
        store.mark_turn_finished(
            store.get_running_turn(thread_id)["managed_turn_id"],
            status="ok",
            assistant_text="Completed",
        )

        supervisor = HubSupervisor.from_path(hub_env.hub_root)
        try:
            snapshot = asyncio.run(
                build_hub_snapshot(supervisor, hub_root=hub_env.hub_root)
            )
        finally:
            supervisor.shutdown()

        rendered = _render_hub_snapshot(snapshot)

        assert "PMA Managed Threads:" in rendered
        assert "status=reusable" in rendered
        assert "last_turn=completed" in rendered
