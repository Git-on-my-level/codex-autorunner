"""Tests for PMA CLI commands."""

from pathlib import Path
from typing import Optional

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.pma_delivery_targets import (
    PmaDeliveryTargetsStore,
    target_key,
)
from codex_autorunner.surfaces.cli.pma_cli import _resolve_hub_path, pma_app


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

    # File operations
    assert "files" in output, "PMA CLI should have 'files' command"
    assert "upload" in output, "PMA CLI should have 'upload' command"
    assert "download" in output, "PMA CLI should have 'download' command"
    assert "delete" in output, "PMA CLI should have 'delete' command"

    # PMA docs commands from TICKET-007
    assert "docs" in output, "PMA CLI should have 'docs' command group"
    assert "context" in output, "PMA CLI should have 'context' command group"
    assert "thread" in output, "PMA CLI should have 'thread' command group"


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
    assert "send" in output, "PMA thread should have 'send' command"
    assert "turns" in output, "PMA thread should have 'turns' command"
    assert "output" in output, "PMA thread should have 'output' command"
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


def test_pma_cli_thread_list_help_shows_json_option():
    """Verify PMA thread list command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "list", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread list should support --json"


def test_resolve_hub_path_prefers_repo_root_for_config_file(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    assert _resolve_hub_path(tmp_path) == tmp_path.resolve()


def test_pma_thread_list_limits_default_and_max(monkeypatch, tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    captured: dict[str, object] = {}

    def _fake_request_json(
        method: str,
        url: str,
        payload: Optional[dict] = None,
        token_env: Optional[str] = None,
        params: Optional[dict] = None,
    ) -> dict:
        captured["params"] = dict(params or {})
        return {"threads": []}

    monkeypatch.setattr(
        "codex_autorunner.surfaces.cli.pma_cli._request_json",
        _fake_request_json,
    )

    runner = CliRunner()
    default_result = runner.invoke(
        pma_app,
        ["thread", "list", "--path", str(tmp_path)],
    )
    assert default_result.exit_code == 0
    assert captured.get("params", {}).get("limit") == 50

    capped_result = runner.invoke(
        pma_app,
        ["thread", "list", "--path", str(tmp_path), "--limit", "99"],
    )
    assert capped_result.exit_code == 0
    assert captured.get("params", {}).get("limit") == 50


def test_pma_cli_thread_send_help_shows_json_option():
    """Verify PMA thread send command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["thread", "send", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA thread send should support --json"


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


def test_pma_upload_help():
    """Verify PMA upload command has correct signature."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["upload", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "inbox|outbox" in output, "PMA upload should require box argument"
    assert "FILES" in output, "PMA upload should accept files"
    assert "--json" in output, "PMA upload should support --json output mode"


def test_pma_download_help():
    """Verify PMA download command has correct signature."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["download", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "inbox|outbox" in output, "PMA download should require box argument"
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


def test_pma_active_help_shows_json_option():
    """Verify PMA active command supports JSON output mode."""
    runner = CliRunner()
    result = runner.invoke(pma_app, ["active", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "--json" in output, "PMA active should support --json output mode"


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


def test_pma_targets_group_has_required_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["targets", "--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "list" in output
    assert "add" in output
    assert "rm" in output
    assert "clear" in output


def test_pma_targets_remove_alias_is_not_supported() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["targets", "remove", "web"])
    assert result.exit_code != 0
    assert "No such command 'remove'" in result.output


def test_pma_targets_add_help_lists_concrete_ref_examples() -> None:
    runner = CliRunner()
    result = runner.invoke(pma_app, ["targets", "add", "--help"])
    assert result.exit_code == 0
    assert "telegram:-100123:777" in result.output
    assert "discord:123456789012345678" in result.output
    assert "chat:telegram:-100123:777" in result.output
    assert "local:./notes/pma.md" in result.output


def test_pma_targets_add_list_rm_clear(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    runner = CliRunner()

    add_telegram_result = runner.invoke(
        pma_app,
        ["targets", "add", "telegram:-100123:777", "--path", str(tmp_path)],
    )
    assert add_telegram_result.exit_code == 0
    assert (
        "Added PMA delivery target: chat:telegram:-100123:777"
        in add_telegram_result.stdout
    )

    add_discord_result = runner.invoke(
        pma_app,
        ["targets", "add", "discord:987654321", "--path", str(tmp_path)],
    )
    assert add_discord_result.exit_code == 0
    assert (
        "Added PMA delivery target: chat:discord:987654321" in add_discord_result.stdout
    )

    add_explicit_chat_result = runner.invoke(
        pma_app,
        ["targets", "add", "chat:discord:12345", "--path", str(tmp_path)],
    )
    assert add_explicit_chat_result.exit_code == 0
    assert (
        "Added PMA delivery target: chat:discord:12345"
        in add_explicit_chat_result.stdout
    )

    add_local_result = runner.invoke(
        pma_app,
        ["targets", "add", "local:./notes/pma.md", "--path", str(tmp_path)],
    )
    assert add_local_result.exit_code == 0
    assert "Added PMA delivery target: local:./notes/pma.md" in add_local_result.stdout

    add_web_result = runner.invoke(
        pma_app,
        ["targets", "add", "web", "--path", str(tmp_path)],
    )
    assert add_web_result.exit_code == 0
    assert "Added PMA delivery target: web" in add_web_result.stdout

    list_result = runner.invoke(pma_app, ["targets", "list", "--path", str(tmp_path)])
    assert list_result.exit_code == 0
    assert "chat:telegram:-100123:777" in list_result.stdout
    assert "chat:discord:987654321" in list_result.stdout
    assert "chat:discord:12345" in list_result.stdout
    assert "local:./notes/pma.md" in list_result.stdout
    assert "web" in list_result.stdout

    rm_result = runner.invoke(
        pma_app,
        ["targets", "rm", "chat:telegram:-100123:777", "--path", str(tmp_path)],
    )
    assert rm_result.exit_code == 0
    assert "Removed PMA delivery target: chat:telegram:-100123:777" in rm_result.stdout

    clear_result = runner.invoke(
        pma_app,
        ["targets", "clear", "--path", str(tmp_path)],
    )
    assert clear_result.exit_code == 0
    assert "Cleared PMA delivery targets." in clear_result.stdout

    state = PmaDeliveryTargetsStore(tmp_path).load()
    keys = {
        key
        for key in (target_key(target) for target in state.get("targets", []))
        if isinstance(key, str)
    }
    assert keys == set()


def test_pma_targets_add_rejects_invalid_ref(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    runner = CliRunner()
    result = runner.invoke(
        pma_app,
        ["targets", "add", "telegram:not-a-number", "--path", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "Invalid target ref" in result.output
    assert "Refs:" in result.output
    assert "web" in result.output
    assert "local:<path>" in result.output
    assert "chat:telegram:<chat_id>[:<thread_id>]" in result.output
