from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.app_server_prompts import (
    TRUNCATION_MARKER,
    build_autorunner_prompt,
    build_doc_chat_prompt,
    build_spec_ingest_prompt,
)
from codex_autorunner.core.config import load_config


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_doc_chat_prompt_limits_and_instructions(repo: Path) -> None:
    config = load_config(repo)  # type: ignore[arg-type]
    limits = config.app_server.prompts.doc_chat
    _write_text(
        config.doc_path("todo"),
        "A" * (limits.target_excerpt_max_chars + 200),
    )
    message = "B" * (limits.message_max_chars + 200)
    recent = "C" * (limits.recent_summary_max_chars + 200)
    docs = {
        key: {
            "content": config.doc_path(key).read_text(encoding="utf-8"),
            "source": "disk",
        }
        for key in ("todo", "progress", "opinions", "spec", "summary")
    }
    prompt = build_doc_chat_prompt(
        config,
        message=message,
        recent_summary=recent,
        docs=docs,
        targets=("todo",),
    )
    assert len(prompt) <= limits.max_chars
    assert "Do NOT write files" in prompt
    assert "<PATCH>" in prompt and "</PATCH>" in prompt
    assert TRUNCATION_MARKER in prompt
    assert ".codex-autorunner/TODO.md" in prompt


def test_spec_ingest_prompt_limits_and_instructions(repo: Path) -> None:
    config = load_config(repo)  # type: ignore[arg-type]
    limits = config.app_server.prompts.spec_ingest
    _write_text(
        config.doc_path("spec"),
        "D" * (limits.spec_excerpt_max_chars + 500),
    )
    message = "E" * (limits.message_max_chars + 500)
    prompt = build_spec_ingest_prompt(config, message=message)
    assert len(prompt) <= limits.max_chars
    assert "Do NOT write files" in prompt
    assert "TODO/PROGRESS/OPINIONS" in prompt
    assert TRUNCATION_MARKER in prompt
    assert ".codex-autorunner/SPEC.md" in prompt


def test_autorunner_prompt_limits_and_instructions(repo: Path) -> None:
    config = load_config(repo)  # type: ignore[arg-type]
    limits = config.app_server.prompts.autorunner
    _write_text(
        config.doc_path("todo"),
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
    assert "Work through TODO items from top to bottom" in prompt
    assert "Do NOT write files" not in prompt
    assert TRUNCATION_MARKER in prompt
    assert ".codex-autorunner/TODO.md" in prompt
