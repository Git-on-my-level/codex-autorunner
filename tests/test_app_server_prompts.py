from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.app_server_prompts import (
    TRUNCATION_MARKER,
    build_autorunner_prompt,
)
from codex_autorunner.core.config import load_repo_config


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_autorunner_prompt_limits_and_instructions(repo: Path) -> None:
    config = load_repo_config(repo)  # type: ignore[arg-type]
    limits = config.app_server.prompts.autorunner
    _write_text(
        config.doc_path("spec"),
        "F" * (limits.todo_excerpt_max_chars + 300),
    )
    message = "G" * (limits.message_max_chars + 300)
    prev = "H" * (limits.prev_run_max_chars + 300)
    prompt = build_autorunner_prompt(
        config,
        message=message,
        prev_run_summary=prev,
    )
    assert len(prompt) <= limits.max_chars
    assert "workspace docs" in prompt or "Workspace docs" in prompt
    assert "Do NOT write files" not in prompt
    assert TRUNCATION_MARKER in prompt
    assert ".codex-autorunner/workspace/spec.md" in prompt
    assert ".codex-autorunner/workspace/active_context.md" in prompt
