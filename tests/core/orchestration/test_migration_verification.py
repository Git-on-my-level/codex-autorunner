from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.orchestration.migrate_legacy_state import (
    backfill_legacy_audit_entries,
    backfill_legacy_automation_state,
    backfill_legacy_pma_lifecycle_events,
    backfill_legacy_queue_state,
    backfill_legacy_thread_state,
    backfill_legacy_transcript_mirrors,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.orchestration.verification import (
    verify_audit_parity,
    verify_automation_parity,
    verify_event_parity,
    verify_migration,
    verify_queue_parity,
    verify_thread_parity,
    verify_transcript_parity,
)
from codex_autorunner.core.pma_automation_persistence import PmaAutomationPersistence
from codex_autorunner.core.pma_automation_types import default_pma_automation_state
from codex_autorunner.core.pma_thread_store import PmaThreadStore


def test_verify_thread_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        results = verify_thread_parity(hub_root, conn)
    assert len(results) >= 3
    assert any(r.check_name == "thread_targets_count" for r in results)


def test_verify_thread_parity_with_data(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    store = PmaThreadStore(hub_root)
    _thread = store.create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        name="TestThread",
    )
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        backfill_legacy_thread_state(hub_root, conn)
        results = verify_thread_parity(hub_root, conn)
    assert any(r.check_name == "thread_targets_count" for r in results)
    thread_result = next(r for r in results if r.check_name == "thread_targets_count")
    assert thread_result.status == "passed"
    assert thread_result.legacy_count == thread_result.new_count


def test_verify_automation_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        results = verify_automation_parity(hub_root, conn)
    assert len(results) >= 3
    assert any(r.check_name == "automation_subscriptions_count" for r in results)


def test_verify_automation_parity_with_data(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    persistence = PmaAutomationPersistence(hub_root)
    state = default_pma_automation_state()
    state["subscriptions"].append(
        {
            "subscription_id": "sub-verif-1",
            "created_at": "2026-03-13T00:00:00Z",
            "updated_at": "2026-03-13T00:00:00Z",
            "state": "active",
            "event_types": ["test_event"],
            "idempotency_key": "sub-verif-key-1",
        }
    )
    state["timers"].append(
        {
            "timer_id": "timer-verif-1",
            "due_at": "2026-03-13T01:00:00Z",
            "created_at": "2026-03-13T00:00:00Z",
            "updated_at": "2026-03-13T00:00:00Z",
            "state": "pending",
            "idempotency_key": "timer-verif-key-1",
        }
    )
    persistence.save(state)
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        backfill_legacy_automation_state(hub_root, conn)
        results = verify_automation_parity(hub_root, conn)
    sub_result = next(
        r for r in results if r.check_name == "automation_subscriptions_count"
    )
    assert sub_result.status == "passed"
    assert sub_result.legacy_count == 1
    assert sub_result.new_count == 1
    idem_result = next(
        r for r in results if r.check_name == "subscription_idempotency_keys"
    )
    assert idem_result.status == "passed"


def test_verify_queue_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        results = verify_queue_parity(hub_root, conn)
    assert len(results) >= 2


def test_verify_transcript_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        result = verify_transcript_parity(hub_root, conn)
    assert result.check_name == "transcript_parity"
    assert result.legacy_transcripts == 0
    assert result.new_transcripts == 0


def test_verify_event_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        results = verify_event_parity(hub_root, conn)
    assert len(results) >= 1
    assert results[0].check_name == "event_projections_count"


def test_verify_audit_parity_empty_legacy(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        result = verify_audit_parity(hub_root, conn)
    assert result.check_name == "audit_entries_count"


def test_verify_migration_summary(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    store = PmaThreadStore(hub_root)
    store.create_thread("codex", workspace_root, repo_id="repo-1", name="Test")
    persistence = PmaAutomationPersistence(hub_root)
    persistence.save(default_pma_automation_state())
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        backfill_legacy_thread_state(hub_root, conn)
        backfill_legacy_automation_state(hub_root, conn)
        summary = verify_migration(hub_root, conn)
    assert summary.run_id
    assert summary.started_at
    assert summary.finished_at
    assert summary.status in ("passed", "failed")
    assert "recommendations" in summary.to_dict()
    assert summary.rollback_available


def test_verify_migration_with_all_data(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)
    store = PmaThreadStore(hub_root)
    thread = store.create_thread(
        "codex", workspace_root, repo_id="repo-1", name="FullTest"
    )
    _turn = store.create_turn(thread["managed_thread_id"], prompt="test", model="gpt-4")
    persistence = PmaAutomationPersistence(hub_root)
    state = default_pma_automation_state()
    state["subscriptions"].append(
        {
            "subscription_id": "sub-full-1",
            "created_at": "2026-03-13T00:00:00Z",
            "updated_at": "2026-03-13T00:00:00Z",
            "state": "active",
            "event_types": ["test"],
        }
    )
    persistence.save(state)
    queue_dir = hub_root / ".codex-autorunner" / "pma" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / "test.jsonl").write_text(
        '{"item_id": "item-1", "lane_id": "lane-1", "state": "pending", "idempotency_key": "item-key-1"}\n'
    )
    audit_log = hub_root / ".codex-autorunner" / "pma" / "audit_log.jsonl"
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text(
        '{"entry_id": "audit-1", "action_type": "test", "timestamp": "2026-03-13T00:00:00Z"}\n'
    )
    lifecycle_log = hub_root / ".codex-autorunner" / "pma" / "lifecycle_events.jsonl"
    lifecycle_log.write_text(
        '{"event_id": "event-1", "event_type": "test", "timestamp": "2026-03-13T00:00:00Z"}\n'
    )
    transcripts_dir = hub_root / ".codex-autorunner" / "pma" / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_meta = {
        "turn_id": "trans-1",
        "managed_thread_id": thread["managed_thread_id"],
        "created_at": "2026-03-13T00:00:00Z",
    }
    (transcripts_dir / "trans-1.json").write_text(json.dumps(transcript_meta))
    transcript_content = transcripts_dir / "trans-1.txt"
    transcript_content.write_text("test transcript content")
    with open_orchestration_sqlite(hub_root, durable=False) as conn:
        backfill_legacy_thread_state(hub_root, conn)
        backfill_legacy_automation_state(hub_root, conn)
        backfill_legacy_queue_state(hub_root, conn)
        backfill_legacy_audit_entries(hub_root, conn)
        backfill_legacy_pma_lifecycle_events(hub_root, conn)
        backfill_legacy_transcript_mirrors(hub_root, conn)
        summary = verify_migration(hub_root, conn)
    thread_result = next(
        r for r in summary.thread_parity if r.check_name == "thread_targets_count"
    )
    assert thread_result.status == "passed"
    sub_result = next(
        r
        for r in summary.automation_parity
        if r.check_name == "automation_subscriptions_count"
    )
    assert sub_result.status == "passed"
    queue_result = next(
        r for r in summary.queue_parity if r.check_name == "queue_items_count"
    )
    assert queue_result.status == "passed"
    assert summary.transcript_parity is not None
    assert summary.transcript_parity.status in ("passed", "failed")
    event_result = next(
        r for r in summary.event_parity if r.check_name == "event_projections_count"
    )
    assert event_result.status == "passed"
    assert summary.audit_parity.status == "passed"
    assert summary.overall_passed
    assert "recommendations" in summary.to_dict()
