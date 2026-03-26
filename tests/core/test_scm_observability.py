from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pma_thread_store import PmaThreadStore
from codex_autorunner.core.publish_executor import PublishOperationProcessor
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.publish_operation_executors import (
    build_enqueue_managed_turn_executor,
)
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEventStore
from codex_autorunner.core.scm_reaction_types import ReactionIntent


def _audit_rows(hub_root: Path) -> list[dict[str, object]]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            """
            SELECT action_type, target_kind, target_id, payload_json
              FROM orch_audit_entries
             ORDER BY created_at ASC, audit_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


class _NoopProcessor:
    def process_now(self, limit: int = 10) -> list[object]:
        _ = limit
        return []


def test_scm_observability_records_compact_audit_flow_and_propagates_correlation(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "worktrees" / "feature-observability"
    workspace_root.mkdir(parents=True, exist_ok=True)
    thread = PmaThreadStore(hub_root).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        metadata={"head_branch": "feature/observability"},
    )

    event = ScmEventStore(hub_root).record_event(
        event_id="github:delivery-observe-1",
        provider="github",
        event_type="pull_request_review",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-observe-1",
        correlation_id="corr-scm-observe-1",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add observability coverage.",
            "head_ref": "feature/observability",
            "base_ref": "main",
        },
    )

    service = ScmAutomationService(
        hub_root,
        publish_processor=PublishOperationProcessor(
            PublishJournalStore(hub_root),
            executors={
                "enqueue_managed_turn": build_enqueue_managed_turn_executor(
                    hub_root=hub_root
                )
            },
        ),
    )

    ingested = service.ingest_event(event)

    assert len(ingested.reaction_intents) == 1
    assert len(ingested.publish_operations) == 1
    created = ingested.publish_operations[0]
    assert created.payload["correlation_id"] == "corr-scm-observe-1"
    assert created.payload["scm_reaction"]["correlation_id"] == "corr-scm-observe-1"
    assert (
        created.payload["request"]["metadata"]["scm"]["correlation_id"]
        == "corr-scm-observe-1"
    )

    processed = service.process_now(limit=10)

    assert len(processed) == 1
    assert processed[0].state == "succeeded"
    assert processed[0].response["correlation_id"] == "corr-scm-observe-1"

    client_request_id = str(processed[0].response["client_request_id"])
    turn = PmaThreadStore(hub_root).get_turn_by_client_turn_id(
        str(thread["managed_thread_id"]),
        client_request_id,
    )
    assert turn is not None
    assert turn["client_turn_id"] == client_request_id

    rows = _audit_rows(hub_root)
    action_types = [str(row["action_type"]) for row in rows]
    assert "scm.binding_resolved" in action_types
    assert "scm.routed_intent" in action_types
    assert "scm.publish_created" in action_types
    assert "scm.publish_finished" in action_types

    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        assert payload["correlation_id"] == "corr-scm-observe-1"
        assert "raw_payload" not in payload
        assert "body" not in payload


def test_scm_event_store_round_trips_correlation_id(tmp_path: Path) -> None:
    store = ScmEventStore(tmp_path)

    created = store.record_event(
        event_id="github:delivery-xyz",
        provider="github",
        event_type="pull_request",
        correlation_id="corr-xyz",
        payload={"action": "opened"},
    )
    loaded = store.get_event(created.event_id)
    listed = store.list_events(limit=10)

    assert created.correlation_id == "corr-xyz"
    assert loaded is not None
    assert loaded.correlation_id == "corr-xyz"
    assert listed[0].correlation_id == "corr-xyz"


def test_publish_created_audit_marks_deduped_operations(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    event = ScmEventStore(hub_root).record_event(
        event_id="github:event-dedupe-1",
        provider="github",
        event_type="pull_request_review",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        correlation_id="corr-dedupe",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add observability coverage.",
            "head_ref": "feature/dedupe",
            "base_ref": "main",
        },
    )
    journal = PublishJournalStore(hub_root)
    service = ScmAutomationService(
        hub_root,
        binding_resolver=lambda _event, thread_target_id=None: None,
        reaction_router=lambda event, binding=None, config=None: [
            ReactionIntent(
                reaction_kind="changes_requested",
                operation_kind="notify_chat",
                operation_key="scm:dedupe-key",
                payload={
                    "correlation_id": "corr-dedupe",
                    "delivery": "primary_pma",
                    "message": "Changes requested.",
                    "repo_id": "repo-1",
                },
                event_id=event.event_id,
                binding_id=None,
            )
        ],
        journal=journal,
        publish_processor=_NoopProcessor(),
    )

    service.ingest_event(event)
    service.ingest_event(event)

    rows = [
        json.loads(str(row["payload_json"]))
        for row in _audit_rows(hub_root)
        if row["action_type"] == "scm.publish_created"
    ]
    assert len(rows) >= 2
    assert sorted(bool(row["deduped"]) for row in rows[:2]) == [False, True]
