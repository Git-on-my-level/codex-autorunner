from __future__ import annotations

from pathlib import Path

from codex_autorunner.tickets.files import (
    list_ticket_paths,
    parse_ticket_index,
    read_ticket,
)


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
