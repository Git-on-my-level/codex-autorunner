"""Tests for PMA CLI commands."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.filebox import BOXES
from codex_autorunner.core.pma_audit import PmaActionType, PmaAuditLog
from codex_autorunner.surfaces.cli import pma_cli
from codex_autorunner.surfaces.cli.pma_cli import pma_app


def test_pma_cli_has_required_commands():
    """Verify PMA CLI has all required commands."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["--help"])
    assert result.exit_code == 0
    output = result.stdout

    # Core commands from TICKET-004 scope
    assert "chat" in output, "PMA CLI should have 'chat' command"
    assert "interrupt" in output, "PMA CLI should have 'interrupt' command"
    assert "reset" in output, "PMA CLI should have 'reset' command"
    assert "hygiene" in output, "PMA CLI should have 'hygiene' command"

    # File operations
    assert "file" in output, "PMA CLI should have 'file' command group"
    assert "files" in output, "PMA CLI should have 'files' command"
    assert "upload" in output, "PMA CLI should have 'upload' command"
    assert "download" in output, "PMA CLI should have 'download' command"
    assert "delete" in output, "PMA CLI should have 'delete' command"

    # PMA docs commands from TICKET-007
    assert "docs" in output, "PMA CLI should have 'docs' command group"
    assert "context" in output, "PMA CLI should have 'context' command group"
    assert "thread" in output, "PMA CLI should have 'thread' command group"
    assert "targets" not in output, "PMA CLI should not expose 'targets' commands"


def test_pma_cli_targets_commands_removed() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["targets", "--help"])
    assert result.exit_code != 0
    assert "No such command 'targets'" in result.output


def test_pma_cli_thread_group_has_required_commands():
    """Verify PMA thread command group includes managed-thread commands."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "spawn" in output, "PMA thread should have 'spawn' command"
    assert "create" in output, "PMA thread should have 'create' alias"
    assert "list" in output, "PMA thread should have 'list' command"
    assert "info" in output, "PMA thread should have 'info' command"
    assert "status" in output, "PMA thread should have 'status' command"
    assert "send" in output, "PMA thread should have 'send' command"
    assert "turns" in output, "PMA thread should have 'turns' command"
    assert "output" in output, "PMA thread should have 'output' command"
    assert "tail" in output, "PMA thread should have 'tail' command"
    assert "compact" in output, "PMA thread should have 'compact' command"
    assert "resume" in output, "PMA thread should have 'resume' command"
    assert "archive" in output, "PMA thread should have 'archive' command"
    assert "interrupt" in output, "PMA thread should have 'interrupt' command"


def test_pma_cli_thread_spawn_help_shows_json_option():
    """Verify PMA thread spawn command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "spawn", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread spawn should support --json"
    assert "--notify-on" in output, "PMA thread spawn should support --notify-on"


def test_pma_cli_thread_list_help_shows_json_option():
    """Verify PMA thread list command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "list", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread list should support --json"
    assert "--ndjson" in output, "PMA thread list should support --ndjson"


def test_pma_cli_thread_send_help_shows_json_option():
    """Verify PMA thread send command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "send", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread send should support --json"
    assert "--watch" in output, "PMA thread send should support --watch"
    assert "--if-busy" in output, "PMA thread send should support busy-thread policy"
    assert "--notify-on" in output, "PMA thread send should support --notify-on"
    assert "--message-file" in output, "PMA thread send should support file input"
    assert "--message-stdin" in output, "PMA thread send should support stdin input"


def test_pma_cli_thread_status_help_shows_json_option():
    """Verify PMA thread status command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "status", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread status should support --json"


def test_pma_chat_help_shows_json_option():
    """Verify PMA chat command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["chat", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA chat should support --json output mode"
    assert "--stream" in output, "PMA chat should support streaming"


def test_pma_interrupt_help_shows_json_option():
    """Verify PMA interrupt command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["interrupt", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA interrupt should support --json output mode"


def test_pma_reset_help_shows_json_option():
    """Verify PMA reset command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["reset", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA reset should support --json output mode"


def test_pma_files_help_shows_json_option():
    """Verify PMA files command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["files", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA files should support --json output mode"


def test_pma_file_group_has_required_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["file", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "consume" in output
    assert "dismiss" in output
    assert "restore" in output
    assert "list" in output


def test_pma_upload_help():
    """Verify PMA upload command has correct signature."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["upload", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "|".join(BOXES) in output, "PMA upload should require box argument"
    assert "FILES" in output, "PMA upload should accept files"
    assert "--json" in output, "PMA upload should support --json output mode"


def test_pma_download_help():
    """Verify PMA download command has correct signature."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["download", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "|".join(BOXES) in output, "PMA download should require box argument"
    assert "FILENAME" in output, "PMA download should require filename"
    assert "--output" in output, "PMA download should support --output option"


def test_pma_delete_help():
    """Verify PMA delete command has correct signature."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["delete", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "BOX" in output, "PMA delete should support box argument"
    assert "FILENAME" in output, "PMA delete should support filename argument"
    assert "--all" in output, "PMA delete should support --all flag"
    assert "--json" in output, "PMA delete should support --json output mode"


def test_pma_file_commands_help_mentions_filebox():
    """Verify PMA file commands mention FileBox as canonical source."""
    runner = CliRunner()
    for cmd in ["files", "upload", "download", "delete"]:
        result = runner.invoke(pma_app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help should succeed"
        assert (
            "FileBox" in result.stdout
        ), f"{cmd} help should mention FileBox as canonical"


def test_pma_file_lifecycle_commands_round_trip_local_files(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    inbox_file = tmp_path / ".codex-autorunner" / "filebox" / "inbox" / "brief.md"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_text("brief", encoding="utf-8")

    runner = CliRunner()

    consume = runner.invoke(
        pma_app, ["file", "consume", "brief.md", "--path", str(tmp_path)]
    )
    list_consumed = runner.invoke(
        pma_app,
        ["file", "list", "--box", "consumed", "--path", str(tmp_path)],
    )
    restore = runner.invoke(
        pma_app, ["file", "restore", "brief.md", "--path", str(tmp_path)]
    )
    dismiss = runner.invoke(
        pma_app, ["file", "dismiss", "brief.md", "--path", str(tmp_path)]
    )
    list_all = runner.invoke(
        pma_app,
        ["file", "list", "--box", "all", "--path", str(tmp_path), "--json"],
    )

    assert consume.exit_code == 0
    assert "Consumed brief.md -> consumed" in consume.stdout
    assert list_consumed.exit_code == 0
    assert "brief.md" in list_consumed.stdout
    assert restore.exit_code == 0
    assert "Restored brief.md -> inbox" in restore.stdout
    assert dismiss.exit_code == 0
    assert "Dismissed brief.md -> dismissed" in dismiss.stdout
    assert list_all.exit_code == 0
    payload = json.loads(list_all.stdout)
    assert payload["inbox"] == []
    assert payload["consumed"] == []
    assert [entry["name"] for entry in payload["dismissed"]] == ["brief.md"]


def test_pma_active_help_shows_json_option():
    """Verify PMA active command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["active", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA active should support --json output mode"


def test_pma_hygiene_help_shows_apply_and_category_options():
    runner = CliRunner()
    result = runner.invoke(pma_app, ["hygiene", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--apply" in output
    assert "--category" in output
    assert "--json" in output


def test_pma_agents_help_shows_json_option():
    """Verify PMA agents command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["agents", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA agents should support --json output mode"


def test_pma_models_help_shows_json_option():
    """Verify PMA models command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["models", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA models should support --json output mode"
    assert "AGENT" in output, "PMA models should require agent argument"


def test_pma_binding_work_help_uses_busy_work_copy() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["binding", "work", "--help"])
    assert result.exit_code == 0
    assert "running or queued work" in result.stdout


def test_pma_agents_displays_capabilities():
    """Verify PMA agents command displays capabilities."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["agents", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA agents should support --json output mode"


def test_pma_agents_capability_filtering():
    """Verify PMA agents command supports capability filtering."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["agents", "--help"])
    assert result.exit_code == 0


def test_pma_docs_command_group_exists():
    """Verify PMA docs command group exists."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["docs", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "show" in output, "PMA docs should have 'show' command"


def test_pma_docs_show_agents(tmp_path: Path):
    """Verify PMA docs show agents command works."""
    seed_hub_files(tmp_path, force=True)

    runner = CliRunner()
    result = runner.invoke(pma_app, ["docs", "show", "agents", "--path", str(tmp_path)])
    assert result.exit_code == 0
    output = result.stdout
    assert "Durable best-practices" in output
    assert "What belongs here" in output


def test_pma_docs_show_active(tmp_path: Path):
    """Verify PMA docs show active command works."""
    seed_hub_files(tmp_path, force=True)

    runner = CliRunner()
    result = runner.invoke(pma_app, ["docs", "show", "active", "--path", str(tmp_path)])
    assert result.exit_code == 0
    output = result.stdout
    assert "short-lived" in output
    assert "active context" in output.lower()


def test_pma_docs_show_log(tmp_path: Path):
    """Verify PMA docs show log command works."""
    seed_hub_files(tmp_path, force=True)

    runner = CliRunner()
    result = runner.invoke(pma_app, ["docs", "show", "log", "--path", str(tmp_path)])
    assert result.exit_code == 0
    output = result.stdout
    assert "append-only" in output
    assert "context log" in output.lower()


def test_pma_docs_show_invalid_type(tmp_path: Path):
    """Verify PMA docs show rejects invalid doc type."""
    seed_hub_files(tmp_path, force=True)

    runner = CliRunner()
    result = runner.invoke(
        pma_app, ["docs", "show", "invalid", "--path", str(tmp_path)]
    )
    assert result.exit_code == 1
    output = result.output
    assert "Invalid doc_type" in output


def test_pma_context_command_group_exists():
    """Verify PMA context command group exists."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["context", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "reset" in output, "PMA context should have 'reset' command"
    assert "snapshot" in output, "PMA context should have 'snapshot' command"
    assert "prune" in output, "PMA context should have 'prune' command"
    assert "compact" in output, "PMA context should have 'compact' command"


def test_pma_context_reset(tmp_path: Path):
    """Verify PMA context reset command works and is idempotent."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )

    runner = CliRunner()

    result = runner.invoke(pma_app, ["context", "reset", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Reset active_context.md" in result.stdout

    content = active_context_path.read_text(encoding="utf-8")
    assert "short-lived" in content
    assert "Pruning guidance" in content

    result2 = runner.invoke(pma_app, ["context", "reset", "--path", str(tmp_path)])
    assert result2.exit_code == 0
    assert "Reset active_context.md" in result2.stdout


def test_pma_cli_thread_query_commands_use_orchestration_routes(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    responses = {
        "/hub/pma/threads": {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                    "status_reason": "thread_created",
                    "repo_id": "repo-1",
                }
            ]
        },
        "/hub/pma/threads/thread-1": {
            "thread": {
                "managed_thread_id": "thread-1",
                "agent": "codex",
                "status": "completed",
            }
        },
        "/hub/pma/threads/thread-1/status": {
            "managed_thread_id": "thread-1",
            "thread": {
                "managed_thread_id": "thread-1",
                "agent": "codex",
                "repo_id": "repo-1",
                "status": "completed",
                "lifecycle_status": "active",
                "status_reason": "managed_turn_completed",
            },
            "status": "completed",
            "status_reason": "managed_turn_completed",
            "turn": {
                "managed_turn_id": "turn-1",
                "status": "ok",
                "activity": "completed",
                "elapsed_seconds": 60,
                "idle_seconds": 0,
            },
            "active_turn_diagnostics": {
                "managed_turn_id": "turn-1",
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "high",
                "prompt_preview": "Inspect the live thread state",
                "backend_thread_id": "backend-thread-1",
                "backend_turn_id": "backend-turn-1",
                "stream_available": True,
                "last_event_at": "2026-03-17T10:11:12Z",
                "last_event_type": "tool_completed",
                "last_event_summary": "tool: status-check",
                "stalled": False,
                "stall_reason": None,
            },
            "recent_progress": [],
            "latest_output_excerpt": "assistant output",
        },
        "/hub/pma/threads/thread-1/tail": {
            "managed_thread_id": "thread-1",
            "managed_turn_id": "turn-1",
            "turn_status": "ok",
            "activity": "completed",
            "elapsed_seconds": 60,
            "idle_seconds": 0,
            "lifecycle_events": ["turn_started", "turn_completed"],
            "active_turn_diagnostics": {
                "managed_turn_id": "turn-1",
                "request_kind": "message",
                "model": "gpt-5",
                "reasoning": "high",
                "prompt_preview": "Inspect the live thread state",
                "backend_thread_id": "backend-thread-1",
                "backend_turn_id": "backend-turn-1",
                "stream_available": True,
                "last_event_at": "2026-03-17T10:11:12Z",
                "last_event_type": "tool_completed",
                "last_event_summary": "tool: status-check",
                "stalled": False,
                "stall_reason": None,
            },
            "events": [],
        },
    }

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env
        calls.append((method, url, params))
        for suffix, response in responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    list_result = runner.invoke(
        pma_app,
        [
            "thread",
            "list",
            "--agent",
            "codex",
            "--status",
            "idle",
            "--repo",
            "repo-1",
            "--path",
            str(tmp_path),
        ],
    )
    info_result = runner.invoke(
        pma_app, ["thread", "info", "--id", "thread-1", "--path", str(tmp_path)]
    )
    status_result = runner.invoke(
        pma_app, ["thread", "status", "--id", "thread-1", "--path", str(tmp_path)]
    )
    tail_result = runner.invoke(
        pma_app, ["thread", "tail", "--id", "thread-1", "--path", str(tmp_path)]
    )

    assert list_result.exit_code == 0
    assert "thread-1 agent=codex status=idle" in list_result.stdout
    assert info_result.exit_code == 0
    assert '"managed_thread_id": "thread-1"' in info_result.stdout
    assert status_result.exit_code == 0
    assert (
        "id=thread-1 agent=codex repo=repo-1 status=reusable last_turn=completed"
        in status_result.stdout
    )
    assert (
        "active_turn: kind=message model=gpt-5 reasoning=high stream=yes stalled=no"
        in status_result.stdout
    )
    assert "prompt: Inspect the live thread state" in status_result.stdout
    assert (
        "last_event: tool_completed @10:11:12 tool: status-check"
        in status_result.stdout
    )
    assert "latest output:" in status_result.stdout
    assert tail_result.exit_code == 0
    assert "turn=turn-1 status=ok activity=completed" in tail_result.stdout
    assert (
        "active_turn: kind=message model=gpt-5 reasoning=high stream=yes stalled=no"
        in tail_result.stdout
    )

    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": "codex",
                "status": "idle",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "limit": 200,
            },
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1",
            None,
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/status",
            {"limit": 20, "level": "info"},
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/tail",
            {"limit": 50, "level": "info"},
        ),
    ]


def test_pma_cli_thread_compact_dry_run_with_status_filter(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env
        calls.append((method, url, params))
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-1",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                        "chat_bound": True,
                        "cleanup_protected": True,
                    },
                    {
                        "managed_thread_id": "thread-2",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                    },
                ]
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--status",
            "completed",
            "--summary",
            "cleanup",
            "--dry-run",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Dry run summary: 2 threads would be compacted" in result.stdout
    assert "thread-1 agent=codex status=completed" in result.stdout
    assert "chat_bound=yes" in result.stdout
    assert "protected=yes" in result.stdout
    assert "thread-2 agent=codex status=completed" in result.stdout
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            {"status": "completed", "limit": 1000},
        )
    ]

    audit_entries = PmaAuditLog(tmp_path).list_recent(
        action_type=PmaActionType.SESSION_COMPACT
    )
    assert audit_entries
    assert audit_entries[-1].details["mode"] == "dry_run"
    assert audit_entries[-1].details["thread_ids"] == ["thread-1", "thread-2"]


def test_pma_cli_thread_compact_executes_batch_for_completed_threads(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    calls: list[
        tuple[
            str,
            str,
            dict[str, object] | None,
            dict[str, object] | None,
        ]
    ] = []

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env
        calls.append((method, url, payload, params))
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-1",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                        "chat_bound": True,
                        "cleanup_protected": True,
                    },
                    {
                        "managed_thread_id": "thread-2",
                        "agent": "codex",
                        "status": "completed",
                        "status_reason": "managed_turn_completed",
                        "repo_id": "repo-1",
                        "resource_kind": "repo",
                        "resource_id": "repo-1",
                    },
                ]
            }
        if method == "POST" and url.endswith("/hub/pma/threads/thread-1/compact"):
            return {
                "thread": {"managed_thread_id": "thread-1", "compact_seed": "cleanup"}
            }
        if method == "POST" and url.endswith("/hub/pma/threads/thread-2/compact"):
            return {
                "thread": {"managed_thread_id": "thread-2", "compact_seed": "cleanup"}
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--status",
            "completed",
            "--summary",
            "cleanup",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Dry run summary: 2 threads would be compacted" in result.stdout
    assert "Compacted thread-1" in result.stdout
    assert "Compacted thread-2" in result.stdout
    assert "Compacted 2 threads" in result.stdout
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            None,
            {"status": "completed", "limit": 1000},
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/compact",
            {"summary": "cleanup", "reset_backend": True},
            None,
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-2/compact",
            {"summary": "cleanup", "reset_backend": True},
            None,
        ),
    ]

    audit_entries = PmaAuditLog(tmp_path).list_recent(
        action_type=PmaActionType.SESSION_COMPACT
    )
    assert audit_entries
    assert audit_entries[-1].details["mode"] == "result"
    assert audit_entries[-1].details["thread_ids"] == ["thread-1", "thread-2"]


def test_pma_cli_thread_compact_all_requires_force(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".codex-autorunner").mkdir()
    (tmp_path / ".codex-autorunner" / "config.yml").write_text(
        "pma: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "compact",
            "--all",
            "--summary",
            "cleanup",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--force is required" in result.output


def test_pma_cli_thread_send_reports_queued_busy_thread(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, token_env, timeout
        captured["url"] = url
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "queued",
                "execution_state": "queued",
                "managed_turn_id": "turn-2",
                "active_managed_turn_id": "turn-1",
                "queue_depth": 1,
                "delivered_message": "follow up",
                "assistant_text": "",
            },
        )

    monkeypatch.setattr(
        pma_cli, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "follow up",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "send_state=queued managed_turn_id=turn-2" in result.stdout
    assert "active_managed_turn_id=turn-1" in result.stdout
    assert "queue_depth=1" in result.stdout
    assert "delivered message:\nfollow up\n" in result.stdout
    assert captured["url"] == "http://127.0.0.1:4321/hub/pma/threads/thread-1/messages"
    assert captured["payload"] == {
        "message": "follow up",
        "busy_policy": "queue",
        "defer_execution": False,
    }


def test_pma_cli_thread_send_reads_message_from_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}
    message_path = tmp_path / "prompt.md"
    message_path.write_text("literal `glm-5-turbo`\nsecond line\n", encoding="utf-8")

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, token_env, timeout
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "completed",
                "managed_turn_id": "turn-3",
                "delivered_message": payload["message"],
                "assistant_text": "done",
            },
        )

    monkeypatch.setattr(
        pma_cli, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message-file",
            str(message_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert captured["payload"] == {
        "message": "literal `glm-5-turbo`\nsecond line\n",
        "busy_policy": "queue",
        "defer_execution": False,
    }
    assert "delivered message:\nliteral `glm-5-turbo`\nsecond line\n" in result.stdout
    assert "\nassistant:\ndone\n" in result.stdout


def test_pma_cli_thread_send_reads_message_from_stdin(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    captured: dict[str, object] = {}

    def _fake_request_json_with_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        timeout=None,
    ):
        _ = method, url, token_env, timeout
        captured["payload"] = payload
        return (
            200,
            {
                "status": "ok",
                "send_state": "accepted",
                "execution_state": "running",
                "managed_turn_id": "turn-4",
                "delivered_message": payload["message"],
                "assistant_text": "",
            },
        )

    monkeypatch.setattr(
        pma_cli, "_request_json_with_status", _fake_request_json_with_status
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message-stdin",
            "--path",
            str(tmp_path),
        ],
        input="stdin payload with backticks `glm-5-turbo`\n",
    )

    assert result.exit_code == 0
    assert captured["payload"] == {
        "message": "stdin payload with backticks `glm-5-turbo`\n",
        "busy_policy": "queue",
        "defer_execution": False,
    }
    assert (
        "delivered message:\nstdin payload with backticks `glm-5-turbo`\n"
        in result.stdout
    )


def test_pma_cli_thread_send_requires_exactly_one_message_source(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    message_path = tmp_path / "prompt.md"
    message_path.write_text("hello\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "send",
            "--id",
            "thread-1",
            "--message",
            "inline",
            "--message-file",
            str(message_path),
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert (
        "Provide exactly one of --message, --message-file, or --message-stdin."
        in result.output
    )


def test_pma_cli_thread_control_commands_use_orchestration_routes(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env, params
        calls.append((method, url, payload))
        if url.endswith("/hub/pma/threads"):
            return {"thread": {"managed_thread_id": "thread-1"}}
        if url.endswith("/resume"):
            return {
                "thread": {
                    "managed_thread_id": "thread-1",
                }
            }
        if url.endswith("/archive"):
            return {"thread": {"managed_thread_id": "thread-1", "status": "archived"}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)
    monkeypatch.setattr(
        pma_cli,
        "_fetch_agent_capabilities",
        lambda config, path: {"codex": {"durable_threads"}},
    )

    runner = CliRunner()
    create_result = runner.invoke(
        pma_app,
        [
            "thread",
            "create",
            "--agent",
            "codex",
            "--repo",
            "repo-1",
            "--name",
            "CLI thread",
            "--path",
            str(tmp_path),
        ],
    )
    resume_result = runner.invoke(
        pma_app,
        [
            "thread",
            "resume",
            "--id",
            "thread-1",
            "--path",
            str(tmp_path),
        ],
    )
    archive_result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert create_result.exit_code == 0
    assert create_result.stdout.strip() == "thread-1"
    assert resume_result.exit_code == 0
    assert "Resumed thread-1" in resume_result.stdout
    assert archive_result.exit_code == 0
    assert "Archived thread-1" in archive_result.stdout

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": "codex",
                "resource_kind": "repo",
                "resource_id": "repo-1",
                "workspace_root": None,
                "name": "CLI thread",
                "context_profile": "car_ambient",
                "notify_on": None,
                "terminal_followup": None,
                "notify_lane": None,
                "notify_once": True,
            },
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/resume",
            {},
        ),
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads/thread-1/archive",
            None,
        ),
    ]


def test_pma_cli_thread_create_rejects_backend_id_option(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "create",
            "--backend-id",
            "backend-thread-1",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "--backend-id" in result.output


def test_pma_cli_thread_archive_reports_unreachable_host_port(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_connect_error(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        raise httpx.ConnectError(
            "[Errno 1] Operation not permitted", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(pma_cli, "_request_json", _raise_connect_error)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-1." in result.output
    assert "Resolved URL: http://127.0.0.1:4321/hub/pma/threads/thread-1/archive" in (
        result.output
    )
    assert "Failure type: hub host/port unreachable." in result.output
    assert "running in this runtime" in result.output


def test_pma_cli_thread_archive_reports_base_path_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_http_status(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        request = httpx.Request("POST", url)
        response = httpx.Response(404, request=request, json={"detail": "Not Found"})
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(pma_cli, "_request_json", _raise_http_status)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-1", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-1." in result.output
    assert "Failure type: possible base-path mismatch." in result.output
    assert "HTTP status: 404" in result.output
    assert "server.base_path" in result.output


def test_pma_cli_thread_archive_preserves_not_found_detail(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _raise_thread_not_found(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = method, payload, token_env, params
        request = httpx.Request("POST", url)
        response = httpx.Response(
            404, request=request, json={"detail": "Managed thread not found"}
        )
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(pma_cli, "_request_json", _raise_thread_not_found)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "archive", "--id", "thread-missing", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to archive managed PMA thread thread-missing." in result.output
    assert "Failure type: HTTP status 404." in result.output
    assert "Server detail: Managed thread not found" in result.output
    assert "possible base-path mismatch" not in result.output


def test_resolve_hub_path_uses_loaded_hub_root_for_worktree_path(hub_env) -> None:
    resolved = pma_cli._resolve_hub_path(hub_env.repo_root)

    assert resolved == hub_env.hub_root


def test_pma_cli_thread_spawn_defaults_agent_for_agent_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = (
        []
    )

    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = token_env
        calls.append((method, url, payload, params))
        if method == "POST" and url.endswith("/hub/pma/threads"):
            return {
                "thread": {
                    "managed_thread_id": "thread-zc-1",
                    "agent": "zeroclaw",
                    "resource_kind": "agent_workspace",
                    "resource_id": "zc-main",
                }
            }
        if method == "GET" and url.endswith("/hub/pma/threads"):
            return {
                "threads": [
                    {
                        "managed_thread_id": "thread-zc-1",
                        "agent": "zeroclaw",
                        "status": "idle",
                        "status_reason": "thread_created",
                        "resource_kind": "agent_workspace",
                        "resource_id": "zc-main",
                    }
                ]
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    create_result = runner.invoke(
        pma_app,
        [
            "thread",
            "spawn",
            "--resource-kind",
            "agent_workspace",
            "--resource-id",
            "zc-main",
            "--name",
            "ZeroClaw Main",
            "--path",
            str(tmp_path),
        ],
    )
    list_result = runner.invoke(
        pma_app,
        [
            "thread",
            "list",
            "--resource-kind",
            "agent_workspace",
            "--resource-id",
            "zc-main",
            "--path",
            str(tmp_path),
        ],
    )

    assert create_result.exit_code == 0
    assert create_result.stdout.strip() == "thread-zc-1"
    assert list_result.exit_code == 0
    assert "thread-zc-1 agent=zeroclaw status=idle" in list_result.stdout
    assert "owner=agent_workspace:zc-main" in list_result.stdout

    assert calls == [
        (
            "POST",
            "http://127.0.0.1:4321/hub/pma/threads",
            {
                "agent": None,
                "resource_kind": "agent_workspace",
                "resource_id": "zc-main",
                "workspace_root": None,
                "name": "ZeroClaw Main",
                "context_profile": "none",
                "notify_on": None,
                "terminal_followup": None,
                "notify_lane": None,
                "notify_once": True,
            },
            None,
        ),
        (
            "GET",
            "http://127.0.0.1:4321/hub/pma/threads",
            None,
            {
                "resource_kind": "agent_workspace",
                "resource_id": "zc-main",
                "limit": 200,
            },
        ),
    ]


def test_pma_cli_thread_list_json_emits_array(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env, params
        assert method == "GET"
        assert url == "http://127.0.0.1:4321/hub/pma/threads"
        return {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                    "status_reason": "thread_created",
                },
                {
                    "managed_thread_id": "thread-2",
                    "agent": "opencode",
                    "status": "running",
                    "status_reason": "turn_active",
                },
            ]
        }

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--json", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    assert [item["managed_thread_id"] for item in parsed] == ["thread-1", "thread-2"]
    assert all(isinstance(item, dict) for item in parsed)


def test_pma_cli_thread_list_ndjson_emits_one_object_per_line(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    def _fake_request_json(
        method: str,
        url: str,
        payload=None,
        token_env=None,
        params=None,
    ):
        _ = payload, token_env, params
        assert method == "GET"
        assert url == "http://127.0.0.1:4321/hub/pma/threads"
        return {
            "threads": [
                {
                    "managed_thread_id": "thread-1",
                    "agent": "codex",
                    "status": "idle",
                },
                {
                    "managed_thread_id": "thread-2",
                    "agent": "opencode",
                    "status": "running",
                },
            ]
        }

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--ndjson", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    parsed = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert [item["managed_thread_id"] for item in parsed] == ["thread-1", "thread-2"]


def test_pma_cli_thread_list_rejects_json_and_ndjson_together(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["thread", "list", "--json", "--ndjson", "--path", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "Choose only one of --json or --ndjson." in result.output


def test_pma_cli_thread_spawn_rejects_invalid_context_profile(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "thread",
            "spawn",
            "--agent",
            "codex",
            "--repo",
            "repo-1",
            "--context-profile",
            "car-ambent",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--context-profile must be one of: car_core, car_ambient, none" in (
        result.stdout
    )


def test_pma_cli_binding_work_empty_state_uses_busy_copy(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        pma_cli,
        "load_hub_config",
        lambda hub_root: SimpleNamespace(
            server_base_path="",
            server_host="127.0.0.1",
            server_port=4321,
            server_auth_token_env=None,
        ),
    )
    monkeypatch.setattr(
        pma_cli,
        "_request_json",
        lambda method, url, payload=None, token_env=None, params=None: {
            "summaries": []
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["binding", "work", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "No busy work found" in result.stdout


def test_pma_context_snapshot(tmp_path: Path):
    """Verify PMA context snapshot appends with timestamp."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )

    custom_content = "# Custom active context\n\n- Item 1\n- Item 2\n"
    active_context_path.write_text(custom_content, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(pma_app, ["context", "snapshot", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Appended snapshot" in result.stdout

    log_content = context_log_path.read_text(encoding="utf-8")

    assert "## Snapshot:" in log_content, "Snapshot header should be present"
    assert (
        "Custom active context" in log_content
    ), "Active context content should be in log"
    assert "Item 1" in log_content, "Active context items should be in log"


def test_pma_context_prune_under_budget(tmp_path: Path):
    """Verify PMA context prune does nothing when under budget."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )

    custom_content = "# Short content\n\n- Item 1\n- Item 2\n"
    active_context_path.write_text(custom_content, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(pma_app, ["context", "prune", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "no prune needed" in result.stdout

    content = active_context_path.read_text(encoding="utf-8")
    assert "Short content" in content, "Content should be unchanged"


def test_pma_context_prune_over_budget(tmp_path: Path):
    """Verify PMA context prune snapshots and resets when over budget."""
    import yaml

    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )

    long_content = "\n".join([f"Line {i}" for i in range(250)])
    active_context_path.write_text(long_content, encoding="utf-8")

    config_path = tmp_path / ".codex-autorunner" / "config.yml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data.setdefault("pma", {})
    config_data["pma"]["active_context_max_lines"] = 50
    config_path.write_text(
        yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8"
    )

    runner = CliRunner()
    result = runner.invoke(pma_app, ["context", "prune", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "snapshotting and pruning" in result.stdout
    assert "Pruned active_context.md" in result.stdout

    log_content = context_log_path.read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content, "Snapshot should be in log"
    assert "Line 1" in log_content, "Content should be in snapshot"

    active_content = active_context_path.read_text(encoding="utf-8")
    assert (
        "This file was pruned" in active_content
    ), "Active context should have prune note"
    assert "Line 1" not in active_content, "Long content should be removed"


def test_pma_context_compact_dry_run(tmp_path: Path):
    """Verify context compact dry-run reports intent without modifying files."""
    seed_hub_files(tmp_path, force=True)
    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    before = active_context_path.read_text(encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["context", "compact", "--path", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "Dry run: compact active_context.md" in result.stdout
    after = active_context_path.read_text(encoding="utf-8")
    assert before == after


def test_pma_context_compact_snapshots_and_rewrites_active_context(tmp_path: Path):
    """Verify context compact snapshots old context and writes compact active context."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "active_context.md"
    )
    context_log_path = (
        tmp_path / ".codex-autorunner" / "pma" / "docs" / "context_log.md"
    )

    long_content = "\n".join([f"- Important line {i}" for i in range(1, 80)])
    active_context_path.write_text(long_content, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        [
            "context",
            "compact",
            "--path",
            str(tmp_path),
            "--max-lines",
            "24",
            "--summary-lines",
            "4",
        ],
    )
    assert result.exit_code == 0
    assert "Compacted active_context.md" in result.stdout

    log_content = context_log_path.read_text(encoding="utf-8")
    assert "## Snapshot:" in log_content
    assert "Important line 1" in log_content

    compacted = active_context_path.read_text(encoding="utf-8")
    assert "## Current priorities" in compacted
    assert "## Next steps" in compacted
    assert "## Open questions" in compacted
    assert "## Archived context summary" in compacted
    assert len(compacted.splitlines()) <= 24
