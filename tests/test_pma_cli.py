"""Tests for PMA CLI commands."""

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.filebox import BOXES
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
    assert "--summary" in output
    assert "--include-needs-confirmation" in output
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


def test_resolve_hub_path_uses_loaded_hub_root_for_worktree_path(hub_env) -> None:
    resolved = pma_cli._resolve_hub_path(hub_env.repo_root)

    assert resolved == hub_env.hub_root


def test_pma_cli_agents_renders_session_controls_and_commands(
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
        assert url == "http://127.0.0.1:4321/hub/pma/agents"
        return {
            "default": "hermes",
            "agents": [
                {
                    "id": "hermes",
                    "name": "Hermes",
                    "capabilities": ["active_thread_discovery"],
                    "session_controls": {
                        "fork": True,
                        "set_model": True,
                        "set_mode": False,
                        "list_sessions": True,
                    },
                    "advertised_commands": [
                        {"name": "/fork", "description": "Fork current session"},
                        {"name": "/model", "description": "Switch model"},
                    ],
                }
            ],
        }

    monkeypatch.setattr(pma_cli, "_request_json", _fake_request_json)

    runner = CliRunner()
    result = runner.invoke(pma_app, ["agents", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Session controls: fork, list_sessions, set_model" in result.stdout
    assert "/fork: Fork current session" in result.stdout
    assert "/model: Switch model" in result.stdout


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
