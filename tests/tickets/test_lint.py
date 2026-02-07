from __future__ import annotations

from pathlib import Path

from codex_autorunner.tickets.lint import (
    lint_dispatch_frontmatter,
    lint_ticket_directory,
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


def test_lint_ticket_frontmatter_preserves_extra() -> None:
    fm, errors = lint_ticket_frontmatter(
        {
            "agent": "codex",
            "done": False,
            "custom": {"a": 1},
        }
    )
    assert errors == []
    assert fm is not None
    assert fm.extra.get("custom") == {"a": 1}


def test_lint_ticket_frontmatter_rejects_depends_on() -> None:
    fm, errors = lint_ticket_frontmatter(
        {
            "agent": "codex",
            "done": False,
            "depends_on": ["TICKET-001"],
        }
    )
    assert fm is None
    assert any("depends_on" in e for e in errors)


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


def test_lint_ticket_directory_detects_duplicate_indices(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()

    # Create duplicate ticket files with same index
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-001-duplicate.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )

    errors = lint_ticket_directory(ticket_dir)
    assert len(errors) == 1
    assert "001" in errors[0]
    assert "TICKET-001.md" in errors[0]
    assert "TICKET-001-duplicate.md" in errors[0]
    assert "Duplicate ticket index" in errors[0]


def test_lint_ticket_directory_no_duplicates(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()

    # Create tickets with unique indices (suffixes allowed)
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-002-foo.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-003.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )

    errors = lint_ticket_directory(ticket_dir)
    assert errors == []


def test_lint_ticket_directory_multiple_duplicates(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()

    # Create multiple duplicate indices
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-001-copy.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-005.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-005-v2.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "TICKET-005-v3.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )

    errors = lint_ticket_directory(ticket_dir)
    assert len(errors) == 2

    # Verify both duplicates are reported
    error_str = "\n".join(errors)
    assert "001" in error_str
    assert "005" in error_str


def test_lint_ticket_directory_ignores_non_ticket_files(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()

    # Create valid tickets and ignore other files
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (ticket_dir / "README.md").write_text("readme", encoding="utf-8")
    (ticket_dir / "notes.txt").write_text("notes", encoding="utf-8")

    errors = lint_ticket_directory(ticket_dir)
    assert errors == []


def test_lint_ticket_directory_empty_directory(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()

    errors = lint_ticket_directory(ticket_dir)
    assert errors == []


def test_lint_ticket_directory_nonexistent_directory(tmp_path: Path) -> None:
    ticket_dir = tmp_path / "nonexistent"

    errors = lint_ticket_directory(ticket_dir)
    assert errors == []
