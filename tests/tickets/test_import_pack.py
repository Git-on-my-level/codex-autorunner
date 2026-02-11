from __future__ import annotations

import zipfile
from pathlib import Path

from codex_autorunner.tickets.import_pack import import_ticket_pack


def test_import_ticket_pack_strips_depends_on(tmp_path: Path) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    zip_path = tmp_path / "pack.zip"

    content = (
        "---\n"
        "agent: codex\n"
        "done: false\n"
        "depends_on:\n"
        "  - TICKET-000\n"
        "---\n"
        "\n"
        "Body\n"
    )
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TICKET-001.md", content)

    report = import_ticket_pack(
        repo_id="repo",
        repo_root=repo_root,
        ticket_dir=ticket_dir,
        zip_path=zip_path,
        lint=False,
        dry_run=False,
        strip_depends_on=True,
    )
    assert report.ok()
    assert report.created == 1
    assert report.items
    assert report.items[0].warnings

    imported = next(ticket_dir.glob("TICKET-*.md"))
    raw = imported.read_text(encoding="utf-8")
    assert "depends_on" not in raw.split("---", 2)[1]
    assert "CAR ticket-pack note: depends_on=" in raw


def test_import_ticket_pack_warns_on_depends_on_ordering_conflict(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "TICKET-001.md",
            "---\nagent: codex\ndone: false\ndepends_on:\n  - TICKET-002\n---\n\nA\n",
        )
        zf.writestr("TICKET-002.md", "---\nagent: codex\ndone: false\n---\n\nB\n")

    report = import_ticket_pack(
        repo_id="repo",
        repo_root=repo_root,
        ticket_dir=ticket_dir,
        zip_path=zip_path,
        lint=False,
        dry_run=True,
        strip_depends_on=True,
        reconcile_depends_on="warn",
    )
    assert report.ok()
    assert report.depends_on_summary["has_depends_on"] is True
    assert report.depends_on_summary["ordering_conflicts"]
    assert report.depends_on_summary["reconciled"] is False


def test_import_ticket_pack_auto_reconciles_depends_on_order(tmp_path: Path) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "TICKET-001.md",
            "---\nagent: codex\ndone: false\ndepends_on:\n  - TICKET-002\n---\n\nA\n",
        )
        zf.writestr("TICKET-002.md", "---\nagent: codex\ndone: false\n---\n\nB\n")

    report = import_ticket_pack(
        repo_id="repo",
        repo_root=repo_root,
        ticket_dir=ticket_dir,
        zip_path=zip_path,
        lint=False,
        dry_run=False,
        strip_depends_on=True,
        reconcile_depends_on="auto",
    )
    assert report.ok()
    assert report.depends_on_summary["reconciled"] is True
    t1 = (ticket_dir / "TICKET-001.md").read_text(encoding="utf-8")
    t2 = (ticket_dir / "TICKET-002.md").read_text(encoding="utf-8")
    assert "B" in t1
    assert "A" in t2
