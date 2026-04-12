from __future__ import annotations

from dataclasses import asdict

from codex_autorunner.tickets.frontmatter import (
    parse_markdown_frontmatter,
    render_markdown_frontmatter,
    split_markdown_frontmatter,
)
from codex_autorunner.tickets.lint import lint_ticket_frontmatter


def test_split_markdown_frontmatter_absent_returns_body_unchanged() -> None:
    text = "# Hello\n\nNo frontmatter here."
    fm, body = split_markdown_frontmatter(text)
    assert fm is None
    assert body == text


def test_split_markdown_frontmatter_parses_yaml_and_preserves_body() -> None:
    text = """---
ticket_id: tkt_test123
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
    assert data["ticket_id"] == "tkt_test123"
    assert data["done"] is False
    assert parsed_body == body


def test_split_markdown_frontmatter_malformed_is_treated_as_absent() -> None:
    # Missing closing --- should not throw; callers will lint based on empty mapping.
    text = """---
ticket_id: tkt_test123
agent: codex
done: false

# Title
"""
    fm, body = split_markdown_frontmatter(text)
    assert fm is None
    assert body == text


def test_profile_round_trips_through_parse_lint_render() -> None:
    original = {
        "ticket_id": "tkt_roundtrip123",
        "agent": "codex",
        "done": False,
        "profile": "m4-pma",
    }
    rendered = render_markdown_frontmatter(original, "body text")
    data, body = parse_markdown_frontmatter(rendered)
    assert data.get("profile") == "m4-pma"

    fm, errors = lint_ticket_frontmatter(data)
    assert errors == []
    assert fm is not None
    assert fm.profile == "m4-pma"
    assert "body text" in body

    serialized = asdict(fm)
    assert serialized["profile"] == "m4-pma"
    assert "profile" not in serialized["extra"]


def test_ticket_without_profile_round_trips_cleanly() -> None:
    original = {
        "ticket_id": "tkt_noprof123",
        "agent": "codex",
        "done": False,
    }
    rendered = render_markdown_frontmatter(original, "body")
    data, body = parse_markdown_frontmatter(rendered)
    fm, errors = lint_ticket_frontmatter(data)
    assert errors == []
    assert fm is not None
    assert fm.profile is None
    serialized = asdict(fm)
    assert serialized["profile"] is None
