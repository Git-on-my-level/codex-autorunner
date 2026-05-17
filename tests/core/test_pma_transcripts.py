from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.pma_transcripts import (
    PmaTranscriptLegacyBackfill,
    PmaTranscriptStore,
    default_pma_transcripts_dir,
)


def test_pma_transcript_store_reads_mirrored_writes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    store = PmaTranscriptStore(hub_root)

    pointer = store.write_transcript(
        turn_id="turn-1",
        metadata={"repo_id": "repo-1", "user_prompt": "hello"},
        assistant_text="world",
    )

    transcript = PmaTranscriptStore(hub_root).read_transcript(pointer.turn_id)
    recent = PmaTranscriptStore(hub_root).list_recent(limit=10)

    assert transcript is not None
    assert pointer.transcript_mirror_id == "turn-1"
    assert transcript["content"].strip() == "User:\nhello\n\nAssistant:\nworld"
    assert transcript["metadata"]["repo_id"] == "repo-1"
    assert [entry["turn_id"] for entry in recent] == ["turn-1"]
    assert recent[0]["preview"] == "User:\nhello\n\nAssistant:\nworld"
    assert not default_pma_transcripts_dir(hub_root).exists()


def test_pma_transcript_store_does_not_fallback_to_legacy_files(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_dir = default_pma_transcripts_dir(hub_root)
    legacy_dir.mkdir(parents=True)
    content_path = legacy_dir / "legacy.md"
    metadata_path = legacy_dir / "legacy.json"
    content_path.write_text("legacy transcript\n", encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "turn_id": "legacy-turn",
                "created_at": "2026-01-01T00:00:00Z",
                "content_path": str(content_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store = PmaTranscriptStore(hub_root)

    assert store.read_transcript("legacy-turn") is None
    assert store.list_recent(limit=10) == []


def test_pma_transcript_legacy_backfill_imports_files_once(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_dir = default_pma_transcripts_dir(hub_root)
    legacy_dir.mkdir(parents=True)
    content_path = legacy_dir / "legacy.md"
    metadata_path = legacy_dir / "legacy.json"
    content_path.write_text("legacy transcript\n", encoding="utf-8")
    metadata_path.write_text(
        json.dumps(
            {
                "turn_id": "legacy-turn",
                "created_at": "2026-01-01T00:00:00Z",
                "content_path": "legacy.md",
                "managed_thread_id": "thread-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = PmaTranscriptLegacyBackfill(hub_root).run()
    transcript = PmaTranscriptStore(hub_root).read_transcript("legacy-turn")
    recent = PmaTranscriptStore(hub_root).list_recent(limit=10)
    second_result = PmaTranscriptLegacyBackfill(hub_root).run()

    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert result.error_count == 0
    assert second_result.imported_count == 0
    assert second_result.skipped_count == 1
    assert second_result.error_count == 0
    assert transcript is not None
    assert transcript["content"] == "legacy transcript\n"
    assert transcript["metadata"]["managed_thread_id"] == "thread-1"
    assert [entry["turn_id"] for entry in recent] == ["legacy-turn"]
    assert recent[0]["preview"] == "legacy transcript"


def test_pma_transcript_legacy_backfill_reports_errors_and_coverage(
    tmp_path: Path,
    caplog,
) -> None:
    hub_root = tmp_path / "hub"
    legacy_dir = default_pma_transcripts_dir(hub_root)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "malformed.json").write_text("{", encoding="utf-8")
    (legacy_dir / "missing-turn.json").write_text("{}", encoding="utf-8")

    result = PmaTranscriptLegacyBackfill(hub_root).run()
    status = PmaTranscriptStore(hub_root).coverage_status()

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.error_count == 2
    assert status.mirrored_count == 0
    assert status.legacy_metadata_files_count == 2
    assert status.legacy_unmirrored_files_count == 2
    assert status.last_backfill_status is not None
    assert status.last_backfill_status["error_count"] == 2
    assert "Failed to read PMA transcript metadata" in caplog.text
    assert "without turn_id" in caplog.text
