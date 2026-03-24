from __future__ import annotations

from pathlib import Path

from codex_autorunner.tickets.files import (
    list_ticket_paths,
    parse_ticket_index,
    read_ticket,
)
from codex_autorunner.tickets.frontmatter import deterministic_ticket_id


def test_parse_ticket_index_accepts_suffix() -> None:
    assert parse_ticket_index("TICKET-123-foo.md") == 123
    assert parse_ticket_index("TICKET-123.md") == 123
    assert parse_ticket_index("ticket-001-bar.md") == 1
    assert parse_ticket_index("note-001.md") is None


def test_list_ticket_paths_orders_by_index_with_suffix(tmp_path: Path) -> None:
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    (tickets / "TICKET-010-foo.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (tickets / "TICKET-002.md").write_text(
        "---\nagent: codex\ndone: false\n---", encoding="utf-8"
    )
    (tickets / "note.md").write_text("ignore", encoding="utf-8")

    paths = list_ticket_paths(tickets)
    assert [p.name for p in paths] == ["TICKET-002.md", "TICKET-010-foo.md"]


def test_read_ticket_rejects_invalid_filename(tmp_path: Path) -> None:
    ticket_path = tmp_path / "tickets" / "BAD-001.md"
    ticket_path.parent.mkdir()
    ticket_path.write_text("---\nagent: codex\ndone: false\n---", encoding="utf-8")

    doc, errors = read_ticket(ticket_path)
    assert doc is None
    assert any("Invalid ticket filename" in e for e in errors)


def test_read_ticket_assigns_stable_ticket_id_when_missing(tmp_path: Path) -> None:
    ticket_path = tmp_path / ".codex-autorunner" / "tickets" / "TICKET-001-no-id.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nBody\n",
        encoding="utf-8",
    )

    first_doc, first_errors = read_ticket(ticket_path)
    second_doc, second_errors = read_ticket(ticket_path)

    assert first_errors == []
    assert second_errors == []
    assert first_doc is not None
    assert second_doc is not None
    assert first_doc.frontmatter.ticket_id == deterministic_ticket_id(ticket_path)
    assert second_doc.frontmatter.ticket_id == first_doc.frontmatter.ticket_id
