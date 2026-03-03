from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.flows.start_policy import evaluate_ticket_start_policy


def test_evaluate_ticket_start_policy_reports_missing_ticket_dir(
    tmp_path: Path,
) -> None:
    details = evaluate_ticket_start_policy(tmp_path / ".codex-autorunner" / "tickets")
    assert details.has_tickets is False
    assert details.lint_errors == ()


def test_evaluate_ticket_start_policy_collects_ticket_validation_issues(
    tmp_path: Path,
) -> None:
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    # Invalid ticket filename.
    (ticket_dir / "README.md").write_text("noop\n", encoding="utf-8")
    # Duplicate index entries (001 and 001-suffix).
    (ticket_dir / "TICKET-001.md").write_text(
        "---\nagent: codex\ndone: false\n---\n", encoding="utf-8"
    )
    # Invalid frontmatter for a valid ticket filename.
    (ticket_dir / "TICKET-001-extra.md").write_text(
        "---\nagent: \ndone: false\n---\n", encoding="utf-8"
    )

    details = evaluate_ticket_start_policy(ticket_dir)
    assert details.has_tickets is True
    assert any("Invalid ticket filename" in err for err in details.invalid_filenames)
    assert any(
        "duplicate ticket index" in err.lower() for err in details.duplicate_indices
    )
    assert any("agent is required" in err.lower() for err in details.frontmatter)


def test_evaluate_ticket_start_policy_rejects_non_directory_ticket_path(
    tmp_path: Path,
) -> None:
    ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
    ticket_dir.parent.mkdir(parents=True, exist_ok=True)
    ticket_dir.write_text("not-a-directory", encoding="utf-8")

    details = evaluate_ticket_start_policy(ticket_dir)
    assert details.has_tickets is False
    assert details.duplicate_indices == ()
    assert details.frontmatter == ()
    assert any(
        "ticket path must be a directory" in err.lower()
        for err in details.invalid_filenames
    )
