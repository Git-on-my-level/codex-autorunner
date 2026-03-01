from __future__ import annotations

import logging
from pathlib import Path

from codex_autorunner.tickets.spec_ingest import ingest_workspace_spec_to_tickets


def test_spec_ingest_receipt_write_failure_is_non_fatal(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    repo_root = tmp_path
    spec_path = repo_root / ".codex-autorunner" / "contextspace" / "spec.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("# Spec\n", encoding="utf-8")

    def _fail_receipt(*_args, **_kwargs) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(
        "codex_autorunner.tickets.spec_ingest.write_ingest_receipt", _fail_receipt
    )

    with caplog.at_level(logging.WARNING):
        result = ingest_workspace_spec_to_tickets(repo_root)

    assert result.created == 1
    assert result.first_ticket_path == ".codex-autorunner/tickets/TICKET-001.md"
    assert (repo_root / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert not (
        repo_root / ".codex-autorunner" / "tickets" / "ingest_state.json"
    ).exists()
    assert "Failed to write ingest receipt" in caplog.text
