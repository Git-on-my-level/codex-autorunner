"""Tests for PMA CLI commands."""

from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.bootstrap import seed_hub_files
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

    # File operations
    assert "files" in output, "PMA CLI should have 'files' command"
    assert "upload" in output, "PMA CLI should have 'upload' command"
    assert "download" in output, "PMA CLI should have 'download' command"
    assert "delete" in output, "PMA CLI should have 'delete' command"

    # PMA docs commands from TICKET-007
    assert "docs" in output, "PMA CLI should have 'docs' command group"
    assert "context" in output, "PMA CLI should have 'context' command group"


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


def test_pma_context_reset(tmp_path: Path):
    """Verify PMA context reset command works and is idempotent."""
    seed_hub_files(tmp_path, force=True)

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"

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

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"
    context_log_path = tmp_path / ".codex-autorunner" / "pma" / "context_log.md"

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

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"

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

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"
    context_log_path = tmp_path / ".codex-autorunner" / "pma" / "context_log.md"

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
