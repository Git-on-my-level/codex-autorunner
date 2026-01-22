from __future__ import annotations

from codex_autorunner.tickets.frontmatter import (
    parse_markdown_frontmatter,
    split_markdown_frontmatter,
)


def test_split_markdown_frontmatter_absent_returns_body_unchanged() -> None:
    text = "# Hello\n\nNo frontmatter here."
    fm, body = split_markdown_frontmatter(text)
    assert fm is None
    assert body == text


def test_split_markdown_frontmatter_parses_yaml_and_preserves_body() -> None:
    text = """---
agent: codex
done: false
---

# Title

Body.
"""
    fm, body = split_markdown_frontmatter(text)
    assert fm is not None
    assert "agent: codex" in fm
    # split_markdown_frontmatter ensures the body begins with a leading newline.
    assert body.startswith("\n")
    assert "# Title" in body

    data, parsed_body = parse_markdown_frontmatter(text)
    assert data["agent"] == "codex"
    assert data["done"] is False
    assert parsed_body == body


def test_split_markdown_frontmatter_malformed_is_treated_as_absent() -> None:
    # Missing closing --- should not throw; callers will lint based on empty mapping.
    text = """---
agent: codex
done: false

# Title
"""
    fm, body = split_markdown_frontmatter(text)
    assert fm is None
    assert body == text
