from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.core.lifecycle_events import LifecycleEventStore
from codex_autorunner.core.orchestration.migrate_legacy_state import (
    LEGACY_PMA_AUDIT_LOG_PATH,
    LEGACY_PMA_LIFECYCLE_LOG_PATH,
    backfill_legacy_audit_entries,
    backfill_legacy_pma_lifecycle_events,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_audit import PmaActionType, PmaAuditLog
from codex_autorunner.core.pma_lifecycle import PmaLifecycleRouter


def test_backfill_legacy_audit_and_pma_lifecycle_jsonl_into_orchestration_sqlite(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / LEGACY_PMA_AUDIT_LOG_PATH
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "entry_id": "audit-1",
                "action_type": "chat_completed",
                "timestamp": "2026-03-13T14:00:00+00:00",
                "agent": "codex",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "client_turn_id": "client-1",
                "details": {"status": "ok"},
                "status": "ok",
                "error": None,
                "fingerprint": "fp-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    lifecycle_path = tmp_path / LEGACY_PMA_LIFECYCLE_LOG_PATH
    lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_path.write_text(
        json.dumps(
            {
                "event_id": "pma-life-1",
                "event_type": "pma_lifecycle_reset",
                "timestamp": "2026-03-13T14:05:00+00:00",
                "agent": "codex",
                "artifact_path": "/tmp/artifact.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with open_orchestration_sqlite(tmp_path) as conn:
        audit_counts = backfill_legacy_audit_entries(tmp_path, conn)
        lifecycle_counts = backfill_legacy_pma_lifecycle_events(tmp_path, conn)

    assert audit_counts == {"entries": 1}
    assert lifecycle_counts == {"events": 1}

    entries = PmaAuditLog(tmp_path).list_recent(
        limit=5, action_type=PmaActionType.CHAT_COMPLETED
    )
    assert len(entries) == 1
    assert entries[0].entry_id == "audit-1"
    assert entries[0].thread_id == "thread-1"
    assert entries[0].fingerprint == "fp-1"

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT event_type, processed, payload_json
              FROM orch_event_projections
             WHERE event_family = 'pma.lifecycle'
               AND event_id = ?
            """,
            ("pma-life-1",),
        ).fetchone()

    assert row is not None
    assert str(row["event_type"]) == "pma_lifecycle_reset"
    assert int(row["processed"]) == 1
    assert json.loads(str(row["payload_json"]))["artifact_path"] == "/tmp/artifact.json"


def test_lifecycle_store_projects_legacy_json_into_orchestration_sqlite(
    tmp_path: Path,
) -> None:
    legacy_json_path = tmp_path / ".codex-autorunner" / "lifecycle_events.json"
    legacy_json_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_json_path.write_text(
        json.dumps(
            [
                {
                    "event_id": "legacy-life-1",
                    "event_type": "flow_failed",
                    "repo_id": "repo-1",
                    "run_id": "run-1",
                    "data": {"error": "boom"},
                    "origin": "runner",
                    "timestamp": "2026-03-13T14:10:00+00:00",
                    "processed": False,
                }
            ]
        ),
        encoding="utf-8",
    )

    store = LifecycleEventStore(tmp_path)
    unprocessed = store.get_unprocessed(limit=10)

    assert len(unprocessed) == 1
    assert unprocessed[0].event_id == "legacy-life-1"
    assert store.path.name == "orchestration.sqlite3"

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT event_type, processed, payload_json
              FROM orch_event_projections
             WHERE event_family = 'lifecycle'
               AND event_id = ?
            """,
            ("legacy-life-1",),
        ).fetchone()

    assert row is not None
    assert str(row["event_type"]) == "flow_failed"
    assert int(row["processed"]) == 0
    assert json.loads(str(row["payload_json"]))["origin"] == "runner"


@pytest.mark.asyncio
async def test_pma_lifecycle_router_projects_events_into_orchestration_sqlite(
    tmp_path: Path,
) -> None:
    router = PmaLifecycleRouter(tmp_path)

    await router.reset(agent="opencode")

    with open_orchestration_sqlite(tmp_path) as conn:
        row = conn.execute(
            """
            SELECT event_type, event_family, processed
              FROM orch_event_projections
             WHERE event_family = 'pma.lifecycle'
             ORDER BY rowid DESC
             LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert str(row["event_family"]) == "pma.lifecycle"
    assert str(row["event_type"]) == "pma_lifecycle_reset"
    assert int(row["processed"]) == 1
