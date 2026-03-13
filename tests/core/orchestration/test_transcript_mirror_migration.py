from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.orchestration.migrate_legacy_state import (
    LEGACY_PMA_TRANSCRIPTS_DIR,
    backfill_legacy_transcript_mirrors,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore


def test_backfill_legacy_transcript_files_into_orchestration_sqlite(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path
    transcripts_dir = hub_root / LEGACY_PMA_TRANSCRIPTS_DIR
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    content_path = transcripts_dir / "20260313T140000Z_turn-1.md"
    metadata_path = transcripts_dir / "20260313T140000Z_turn-1.json"
    content_path.write_text("assistant transcript payload\n", encoding="utf-8")
    metadata = {
        "turn_id": "turn-1",
        "managed_thread_id": "thread-1",
        "managed_turn_id": "turn-1",
        "repo_id": "repo-1",
        "agent": "codex",
        "model": "gpt-5",
        "created_at": "2026-03-13T14:00:00+00:00",
        "content_path": str(content_path),
        "metadata_path": str(metadata_path),
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with open_orchestration_sqlite(hub_root) as conn:
        counts = backfill_legacy_transcript_mirrors(hub_root, conn)

    assert counts == {"transcripts": 1}

    store = PmaTranscriptStore(hub_root)
    recent = store.list_recent(limit=5)
    assert len(recent) == 1
    assert recent[0]["turn_id"] == "turn-1"
    assert recent[0]["preview"] == "assistant transcript payload"

    transcript = store.read_transcript("turn-1")
    assert transcript is not None
    assert transcript["metadata"]["managed_thread_id"] == "thread-1"
    assert transcript["content"].strip() == "assistant transcript payload"


def test_transcript_writes_filter_non_plain_mirror_metadata(tmp_path: Path) -> None:
    store = PmaTranscriptStore(tmp_path)

    store.write_transcript(
        turn_id="turn-2",
        metadata={
            "turn_id": "turn-2",
            "agent": "codex",
            "repo_id": "repo-2",
            "user_prompt": "hello world",
            "tool_payload": {"secret": "omit-me"},
            "media": {"blob": "omit-me-too"},
            "lifecycle_event": {
                "event_id": "event-1",
                "event_type": "flow_completed",
            },
        },
        assistant_text="assistant output",
    )

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT metadata_json, text_content
              FROM orch_transcript_mirrors
             WHERE transcript_mirror_id = ?
            """,
            ("turn-2",),
        ).fetchone()

    assert row is not None
    metadata = json.loads(str(row["metadata_json"]))
    assert metadata["turn_id"] == "turn-2"
    assert metadata["user_prompt"] == "hello world"
    assert metadata["lifecycle_event"]["event_id"] == "event-1"
    assert "tool_payload" not in metadata
    assert "media" not in metadata
    assert str(row["text_content"]) == "assistant output"
