from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from codex_autorunner.tickets.pack_import import setup_ticket_pack


def test_setup_ticket_pack_receipt_write_failure_is_non_fatal(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TICKET-001.md", "---\nagent: codex\ndone: false\n---\n\nA\n")

    def _fail_receipt(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "codex_autorunner.tickets.pack_import.write_ingest_receipt", _fail_receipt
    )

    with caplog.at_level(logging.WARNING):
        report = setup_ticket_pack(target_path=repo_root, zip_path=zip_path)

    assert report.extracted_files == ["TICKET-001.md"]
    assert (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert not (
        repo_root / ".codex-autorunner" / "tickets" / "ingest_state.json"
    ).exists()
    assert "Failed to write ingest receipt" in caplog.text
