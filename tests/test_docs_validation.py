from __future__ import annotations

from codex_autorunner.core.docs import validate_todo_markdown


def test_validate_todo_markdown_allows_empty_or_heading_only() -> None:
    assert validate_todo_markdown("") == []
    assert validate_todo_markdown("# TODO\n\n") == []


def test_validate_todo_markdown_rejects_plain_bullets() -> None:
    errors = validate_todo_markdown("# TODO\n\n- do the thing\n")
    assert errors


def test_validate_todo_markdown_accepts_checkbox_bullets() -> None:
    errors = validate_todo_markdown("# TODO\n\n- [ ] do the thing\n- [x] done thing\n")
    assert errors == []
