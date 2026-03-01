from __future__ import annotations

import json
import logging
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

    receipt_path = ticket_dir / "ingest_state.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1
    assert receipt["ingested"] is True
    assert receipt["source"] == "import_pack"
    assert receipt["ingested_at"]


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
    assert not (ticket_dir / "ingest_state.json").exists()


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


def test_import_ticket_pack_receipt_write_failure_is_non_fatal(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TICKET-001.md", "---\nagent: codex\ndone: false\n---\n\nA\n")

    def _fail_receipt(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "codex_autorunner.tickets.import_pack.write_ingest_receipt", _fail_receipt
    )

    with caplog.at_level(logging.WARNING):
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
    assert (ticket_dir / "TICKET-001.md").exists()
    assert not (ticket_dir / "ingest_state.json").exists()
    assert "Failed to write ingest receipt" in caplog.text
