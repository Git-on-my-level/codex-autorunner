from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.chat_document_browser import (
    browser_page_slice,
    list_document_browser_items,
    read_document_browser_document,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_ticket_browser_uses_short_stable_document_ids(tmp_path: Path) -> None:
    repo_root = tmp_path
    _write(
        repo_root / ".codex-autorunner" / "tickets" / f"TICKET-001-{'x' * 120}.md",
        '---\nticket_id: "tkt_alpha"\nagent: codex\ntitle: Alpha task\ndone: false\n---\n\nAlpha body\n',
    )
    _write(
        repo_root / ".codex-autorunner" / "tickets" / "TICKET-002.md",
        '---\nticket_id: "tkt_beta"\nagent: codex\ntitle: Beta task\ndone: true\n---\n\nBeta body\n',
    )

    items = list_document_browser_items(repo_root, "tickets", query="beta")

    assert len(items) == 1
    assert items[0].document_id == "2"
    assert items[0].label == "TICKET-002.md - Beta task"
    assert items[0].description.startswith("done - ")

    document = read_document_browser_document(repo_root, "tickets", "2")
    assert document is not None
    assert document.document_id == "2"
    assert document.rel_path == ".codex-autorunner/tickets/TICKET-002.md"
    assert "Beta body" in document.content


def test_ticket_browser_disambiguates_shared_index(tmp_path: Path) -> None:
    """Same numeric index in two filenames must not collapse to one document_id."""
    repo_root = tmp_path
    _write(
        repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md",
        '---\nticket_id: "tkt_one"\nagent: codex\ntitle: One\ndone: false\n---\n\nOne\n',
    )
    _write(
        repo_root / ".codex-autorunner" / "tickets" / "TICKET-001-draft.md",
        '---\nticket_id: "tkt_draft"\nagent: codex\ntitle: Draft\ndone: false\n---\n\nDraft\n',
    )

    items = list_document_browser_items(repo_root, "tickets")
    ids = sorted(item.document_id for item in items)
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert all(not id_.isdigit() or "-" in id_ for id_ in ids)

    doc_std = read_document_browser_document(repo_root, "tickets", ids[0])
    doc_draft = read_document_browser_document(repo_root, "tickets", ids[1])
    assert doc_std is not None and doc_draft is not None
    assert doc_std.rel_path != doc_draft.rel_path
    assert "TICKET-001.md" in doc_std.rel_path or "TICKET-001.md" in doc_draft.rel_path
    assert (
        "TICKET-001-draft.md" in doc_std.rel_path
        or "TICKET-001-draft.md" in doc_draft.rel_path
    )
    assert "\nOne\n" in doc_std.content or "\nOne\n" in doc_draft.content
    assert "\nDraft\n" in doc_std.content or "\nDraft\n" in doc_draft.content


def test_contextspace_browser_reads_existing_and_missing_docs(tmp_path: Path) -> None:
    repo_root = tmp_path
    _write(
        repo_root / ".codex-autorunner" / "contextspace" / "spec.md",
        "Spec body\n",
    )

    items = list_document_browser_items(repo_root, "contextspace")
    labels = {item.document_id: item.label for item in items}

    assert labels == {
        "active_context": "Active Context",
        "decisions": "Decisions",
        "spec": "Spec",
    }

    spec = read_document_browser_document(repo_root, "contextspace", "spec")
    assert spec is not None
    assert spec.exists is True
    assert spec.rel_path == ".codex-autorunner/contextspace/spec.md"
    assert spec.content == "Spec body\n"

    active = read_document_browser_document(repo_root, "contextspace", "active_context")
    assert active is not None
    assert active.exists is False
    assert active.content == ""


def test_browser_page_slice_clamps_page_bounds() -> None:
    normalized_page, total_pages, items = browser_page_slice(
        ["a", "b", "c"],
        page=9,
        page_size=2,
    )

    assert normalized_page == 1
    assert total_pages == 2
    assert items == ["c"]
