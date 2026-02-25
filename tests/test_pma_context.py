import asyncio
from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.core.pma_context import (
    build_hub_snapshot,
    format_pma_prompt,
    get_active_context_auto_prune_meta,
)
from codex_autorunner.core.pma_thread_store import PmaThreadStore


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
    assert first["status"] == "active"
    assert "last_message_preview" in first

    rendered = _render_hub_snapshot(snapshot)
    assert "PMA Managed Threads:" in rendered
    assert managed_thread_id in rendered
    assert f"repo_id={hub_env.repo_id}" in rendered
    assert "agent=codex" in rendered
    assert "status=active" in rendered


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
    assert "Legacy paths" in result
    assert ".codex-autorunner/pma/inbox/" in result


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
