from pathlib import Path

import yaml

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.pma_context import format_pma_prompt


def _write_hub_config(hub_root: Path, data: dict) -> None:
    """Helper to write hub config to .codex-autorunner/config.yml."""
    config_path = hub_root / ".codex-autorunner" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_format_pma_prompt_includes_workspace_docs(tmp_path: Path) -> None:
    """Test that format_pma_prompt with hub_root includes the workspace docs block."""
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

    assert "<AGENTS.md>" in result
    assert "</AGENTS.md>" in result
    assert "Durable best-practices" in result


def test_format_pma_prompt_includes_active_context_section(tmp_path: Path) -> None:
    """Test that active_context.md content is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<active_context.md>" in result
    assert "</active_context.md>" in result
    assert "short-lived" in result


def test_format_pma_prompt_includes_budget_metadata(tmp_path: Path) -> None:
    """Test that active_context_budget metadata is included in the prompt."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<active_context_budget" in result
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

    assert "<context_log_tail.md>" in result
    assert "</context_log_tail.md>" in result
    assert "append-only" in result


def test_format_pma_prompt_without_hub_root(tmp_path: Path) -> None:
    """Test that format_pma_prompt without hub_root does not include workspace docs."""
    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=None)

    assert "<pma_workspace_docs>" not in result
    assert "</pma_workspace_docs>" not in result


def test_truncation_applied_to_long_agents(tmp_path: Path) -> None:
    """Test that long AGENTS.md content is truncated."""
    seed_hub_files(tmp_path, force=True)

    agents_path = tmp_path / ".codex-autorunner" / "pma" / "AGENTS.md"
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

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"
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

    context_log_path = tmp_path / ".codex-autorunner" / "pma" / "context_log.md"
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

    assert "<context_log_tail.md>" in result
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

    context_log_path = tmp_path / ".codex-autorunner" / "pma" / "context_log.md"
    log_lines = ["line 1", "line 2", "line 3"]
    context_log_path.write_text("\n".join(log_lines), encoding="utf-8")

    snapshot = {"test": "data"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<context_log_tail.md>" in result
    assert "</context_log_tail.md>" in result
    # Extract just the context_log_tail section
    start_idx = result.find("<context_log_tail.md>")
    end_idx = result.find("</context_log_tail.md>")
    context_section = result[start_idx : end_idx + len("</context_log_tail.md>")]
    # With 1 tail line, only the last line should be present
    assert "line 3" in context_section
    assert "line 1" not in context_section
    assert "line 2" not in context_section


def test_format_pma_prompt_includes_hub_snapshot_and_message(tmp_path: Path) -> None:
    """Test that hub_snapshot and user_message sections are always included."""
    seed_hub_files(tmp_path, force=True)

    snapshot = {"test": "data", "foo": "bar"}
    base_prompt = "Base prompt"
    message = "User message"

    result = format_pma_prompt(base_prompt, snapshot, message, hub_root=tmp_path)

    assert "<hub_snapshot>" in result
    assert '"test": "data"' in result
    assert "</hub_snapshot>" in result
    assert "<user_message>" in result
    assert "User message" in result
    assert "</user_message>" in result


def test_format_pma_prompt_with_custom_agent_content(tmp_path: Path) -> None:
    """Test that custom AGENTS.md content is preserved in the prompt."""
    seed_hub_files(tmp_path, force=True)

    agents_path = tmp_path / ".codex-autorunner" / "pma" / "AGENTS.md"
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

    active_context_path = tmp_path / ".codex-autorunner" / "pma" / "active_context.md"
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
