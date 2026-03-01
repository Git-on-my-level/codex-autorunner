from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.tickets.ingest_state import (
    ingest_state_path,
    read_ingest_receipt,
    write_ingest_receipt,
)


def test_write_and_read_ingest_receipt(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    write_ingest_receipt(
        repo_root,
        source="import_pack",
        details={"created": 2},
        ingested_at="2026-02-28T00:00:00+00:00",
    )
    receipt = read_ingest_receipt(repo_root)

    assert receipt is not None
    assert receipt["schema_version"] == 1
    assert receipt["ingested"] is True
    assert receipt["source"] == "import_pack"
    assert receipt["ingested_at"] == "2026-02-28T00:00:00+00:00"
    assert receipt["details"] == {"created": 2}


def test_read_ingest_receipt_returns_none_for_invalid_payload(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    path = ingest_state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ingested": True,
                "ingested_at": "not-an-iso",
                "source": "import_pack",
            }
        ),
        encoding="utf-8",
    )

    assert read_ingest_receipt(repo_root) is None
