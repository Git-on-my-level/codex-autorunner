from __future__ import annotations

from codex_autorunner.tickets.lint import (
    lint_dispatch_frontmatter,
    lint_ticket_frontmatter,
)


def test_lint_ticket_frontmatter_requires_agent_and_done() -> None:
    fm, errors = lint_ticket_frontmatter({})
    assert fm is None
    assert errors

    fm, errors = lint_ticket_frontmatter({"agent": "codex"})
    assert fm is None
    assert any("done" in e for e in errors)

    fm, errors = lint_ticket_frontmatter({"done": False})
    assert fm is None
    assert any("agent" in e for e in errors)


def test_lint_ticket_frontmatter_accepts_known_agents_and_user() -> None:
    fm, errors = lint_ticket_frontmatter({"agent": "codex", "done": False})
    assert errors == []
    assert fm is not None
    assert fm.agent == "codex"

    fm, errors = lint_ticket_frontmatter({"agent": "opencode", "done": True})
    assert errors == []
    assert fm is not None
    assert fm.agent == "opencode"

    fm, errors = lint_ticket_frontmatter({"agent": "user", "done": False})
    assert errors == []
    assert fm is not None
    assert fm.agent == "user"

    fm, errors = lint_ticket_frontmatter({"agent": "unknown", "done": False})
    assert fm is None
    assert any("invalid" in e for e in errors)


def test_lint_ticket_frontmatter_normalizes_requires_and_preserves_extra() -> None:
    fm, errors = lint_ticket_frontmatter(
        {
            "agent": "codex",
            "done": False,
            "requires": [" SPEC.md ", "SPEC.md", "", 123, "foo/bar.md"],
            "custom": {"a": 1},
        }
    )
    assert errors == []
    assert fm is not None
    assert fm.requires == ("SPEC.md", "foo/bar.md")
    assert fm.extra.get("custom") == {"a": 1}


def test_lint_dispatch_frontmatter_defaults_notify_and_validates_mode() -> None:
    normalized, errors = lint_dispatch_frontmatter({})
    assert errors == []
    assert normalized["mode"] == "notify"

    normalized, errors = lint_dispatch_frontmatter({"mode": "PAUSE"})
    assert errors == []
    assert normalized["mode"] == "pause"

    # turn_summary is valid (used for agent turn output)
    normalized, errors = lint_dispatch_frontmatter({"mode": "turn_summary"})
    assert errors == []
    assert normalized["mode"] == "turn_summary"

    normalized, errors = lint_dispatch_frontmatter({"mode": "bad"})
    assert errors
