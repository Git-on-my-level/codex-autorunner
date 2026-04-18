"""Characterization tests freezing SCM single-owner invariants.

These tests pin the canonical ownership boundaries between:
  - orch_scm_events       (ScmEventStore)
  - orch_pr_bindings      (PrBindingStore)
  - orch_scm_polling_watches (ScmPollingWatchStore)
  - orch_publish_operations / orch_publish_attempts (PublishJournalStore)
  - orch_reaction_state   (ScmReactionStateStore)

If future refactors drift ownership (e.g., an adapter starts writing its own
side store), these tests will fail and force an explicit decision.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.pr_bindings import PrBinding, PrBindingStore
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEvent, ScmEventStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.core.scm_reaction_router import route_scm_reactions
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore
from codex_autorunner.core.scm_reaction_types import ReactionIntent

_PAST_TS = "2020-01-01T00:00:00Z"
_FAR_FUTURE_TS = "2099-01-01T00:00:00Z"


def _insert_thread_target(hub_root: Path, thread_target_id: str) -> None:
    with open_orchestration_sqlite(hub_root) as conn:
        conn.execute(
            """
            INSERT INTO orch_thread_targets (
                thread_target_id, agent_id, created_at, updated_at
            ) VALUES (?, 'codex', ?, ?)
            """,
            (thread_target_id, _PAST_TS, _PAST_TS),
        )


def _event(
    *,
    event_id: str = "inv:event-1",
    event_type: str = "pull_request_review",
    pr_number: int = 42,
    payload: dict[str, object] | None = None,
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type=event_type,
        occurred_at="2026-04-01T00:00:00Z",
        received_at="2026-04-01T00:00:01Z",
        created_at="2026-04-01T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-inv",
        pr_number=pr_number,
        delivery_id="delivery-inv",
        payload=payload
        or {
            "action": "submitted",
            "review_state": "changes_requested",
        },
        raw_payload=None,
    )


def _binding(
    *,
    binding_id: str = "binding-inv",
    pr_number: int = 42,
    head_branch: str = "feature/inv",
    thread_target_id: Optional[str] = "thread-inv",
) -> PrBinding:
    return PrBinding(
        binding_id=binding_id,
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-inv",
        pr_number=pr_number,
        pr_state="open",
        head_branch=head_branch,
        base_branch="main",
        thread_target_id=thread_target_id,
        created_at="2026-04-01T00:00:00Z",
        updated_at="2026-04-01T00:00:00Z",
        closed_at=None,
    )


def _list_tables(hub_root: Path) -> set[str]:
    with open_orchestration_sqlite(hub_root) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {row["name"] for row in rows}


def _table_row_count(hub_root: Path, table: str) -> int:
    with open_orchestration_sqlite(hub_root) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]


class _BindingResolverFake:
    def __init__(self, binding: Optional[PrBinding]) -> None:
        self.binding = binding

    def __call__(
        self, event: ScmEvent, *, thread_target_id: Optional[str] = None
    ) -> Optional[PrBinding]:
        return self.binding


class _EventStoreFake:
    def __init__(self, *events: ScmEvent) -> None:
        self._events = {e.event_id: e for e in events}

    def get_event(self, event_id: str) -> Optional[ScmEvent]:
        return self._events.get(event_id)


class _ReactionRouterFake:
    def __init__(self, intents: list[ReactionIntent]) -> None:
        self.intents = intents

    def __call__(self, event, *, binding=None, config=None):
        return list(self.intents)


class _JournalFake:
    def __init__(self) -> None:
        self.operations_by_key: dict[str, object] = {}

    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[object, bool]:
        existing = self.operations_by_key.get(operation_key)
        if existing is not None:
            return existing, True
        from codex_autorunner.core.publish_journal import PublishOperation

        op = PublishOperation(
            operation_id=f"op-{len(self.operations_by_key) + 1}",
            operation_key=operation_key,
            operation_kind=operation_kind,
            state="pending",
            payload=dict(payload or {}),
            response={},
            created_at="2026-04-01T00:00:10Z",
            updated_at="2026-04-01T00:00:10Z",
        )
        self.operations_by_key[operation_key] = op
        return op, False


class _ProcessorFake:
    def __init__(self, processed=None):
        self.processed = processed or []

    def process_now(self, limit: int = 10):
        return list(self.processed)


class _PermissiveReactionStateFake:
    def compute_reaction_fingerprint(self, event, *, binding, intent):
        return f"{intent.reaction_kind}:{event.event_id}:{intent.operation_kind}"

    def should_emit_reaction(self, *, binding_id, reaction_kind, fingerprint):
        return True

    def mark_reaction_emitted(
        self,
        *,
        binding_id,
        reaction_kind,
        fingerprint,
        event_id=None,
        operation_key=None,
        metadata=None,
    ):
        return object()

    def get_reaction_state(self, *, binding_id, reaction_kind, fingerprint):
        return None

    def mark_reaction_suppressed(
        self, *, binding_id, reaction_kind, fingerprint, event_id=None, metadata=None
    ):
        return object()

    def mark_reaction_escalated(
        self,
        *,
        binding_id,
        reaction_kind,
        fingerprint,
        event_id=None,
        operation_key=None,
        metadata=None,
    ):
        return object()

    def mark_reaction_delivery_failed(
        self,
        *,
        binding_id,
        reaction_kind,
        fingerprint,
        event_id=None,
        error_text=None,
        metadata=None,
    ):
        from types import SimpleNamespace

        return SimpleNamespace(escalated_at=None, delivery_failure_count=1)

    def mark_reaction_delivery_succeeded(
        self,
        *,
        binding_id,
        reaction_kind,
        fingerprint,
        event_id=None,
        operation_key=None,
        metadata=None,
    ):
        return object()

    def resolve_other_active_reactions(
        self,
        *,
        binding_id,
        reaction_kind,
        keep_fingerprint,
        event_id=None,
        metadata=None,
    ):
        return 0


# ---------------------------------------------------------------------------
# 1. Schema invariant tests
# ---------------------------------------------------------------------------


class TestSchemaOwnership:
    """Freeze that the canonical SCM tables exist with the expected schemas."""

    SCM_TABLES = {
        "orch_scm_events",
        "orch_pr_bindings",
        "orch_reaction_state",
        "orch_publish_operations",
        "orch_publish_attempts",
        "orch_scm_polling_watches",
    }

    def test_canonical_scm_tables_exist(self, tmp_path: Path) -> None:
        tables = _list_tables(tmp_path)
        for table in self.SCM_TABLES:
            assert table in tables, f"Missing canonical SCM table: {table}"

    def test_scm_event_primary_key_is_event_id(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            info = conn.execute("PRAGMA table_info(orch_scm_events)").fetchall()
        pk_cols = [row["name"] for row in info if row["pk"]]
        assert pk_cols == ["event_id"]

    def test_pr_binding_unique_on_provider_repo_pr(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            indexes = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='orch_pr_bindings'"
            ).fetchall()
        unique_indexes = [i for i in indexes if i["sql"] and "UNIQUE" in i["sql"]]
        names = {i["name"] for i in unique_indexes}
        assert "idx_orch_pr_bindings_provider_repo_pr" in names

    def test_reaction_state_composite_pk(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            info = conn.execute("PRAGMA table_info(orch_reaction_state)").fetchall()
        pk_cols = [row["name"] for row in info if row["pk"]]
        assert set(pk_cols) == {"binding_id", "reaction_kind", "fingerprint"}

    def test_publish_operations_dedup_unique_index(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            indexes = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='orch_publish_operations'"
            ).fetchall()
        dedup_idx = [
            i for i in indexes if i["name"] == "idx_orch_publish_operations_active_key"
        ]
        assert len(dedup_idx) == 1
        assert "pending" in dedup_idx[0]["sql"]
        assert "running" in dedup_idx[0]["sql"]
        assert "succeeded" in dedup_idx[0]["sql"]

    def test_polling_watch_unique_on_provider_binding(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            indexes = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='orch_scm_polling_watches'"
            ).fetchall()
        unique_idx = [
            i
            for i in indexes
            if i["sql"] and "UNIQUE" in i["sql"] and "provider" in i["sql"]
        ]
        names = {i["name"] for i in unique_idx}
        assert "idx_orch_scm_polling_watches_provider_binding" in names

    def test_polling_watch_references_binding_fk(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            fks = conn.execute(
                "PRAGMA foreign_key_list(orch_scm_polling_watches)"
            ).fetchall()
        fk_tables = {fk["table"] for fk in fks}
        assert "orch_pr_bindings" in fk_tables

    def test_publish_attempts_references_operation_fk(self, tmp_path: Path) -> None:
        with open_orchestration_sqlite(tmp_path) as conn:
            fks = conn.execute(
                "PRAGMA foreign_key_list(orch_publish_attempts)"
            ).fetchall()
        fk_tables = {fk["table"] for fk in fks}
        assert "orch_publish_operations" in fk_tables


# ---------------------------------------------------------------------------
# 2. Event store single-owner invariant
# ---------------------------------------------------------------------------


class TestEventStoreOwnership:
    """ScmEventStore is the sole owner of orch_scm_events writes."""

    def test_event_id_uniqueness_enforced(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        store.record_event(
            provider="github",
            event_type="pull_request",
            repo_slug="acme/test",
            pr_number=1,
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_event(
                provider="github",
                event_type="pull_request",
                repo_slug="acme/test",
                pr_number=1,
                event_id=store.list_events(limit=1)[0].event_id,
            )

    def test_delivery_id_not_unique_multiple_events(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        store.record_event(
            provider="github",
            event_type="pull_request",
            delivery_id="del-1",
            repo_slug="acme/a",
            pr_number=1,
        )
        store.record_event(
            provider="github",
            event_type="pull_request_review",
            delivery_id="del-1",
            repo_slug="acme/a",
            pr_number=1,
        )
        assert len(store.list_events(delivery_id="del-1")) == 2

    def test_event_persistence_survives_reopen(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        event = store.record_event(
            provider="github",
            event_type="push",
            repo_slug="acme/repo",
        )
        reopened = ScmEventStore(tmp_path)
        loaded = reopened.get_event(event.event_id)
        assert loaded is not None
        assert loaded.event_id == event.event_id
        assert loaded.event_type == "push"


# ---------------------------------------------------------------------------
# 3. Binding store single-owner invariant
# ---------------------------------------------------------------------------


class TestBindingStoreOwnership:
    """PrBindingStore is the sole owner of orch_pr_bindings writes."""

    def test_upsert_is_idempotent_on_provider_repo_pr(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        first = store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=7,
            head_branch="feature/a",
        )
        second = store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=7,
            head_branch="feature/a-updated",
        )
        assert first.binding_id == second.binding_id
        assert second.head_branch == "feature/a-updated"
        assert _table_row_count(tmp_path, "orch_pr_bindings") == 1

    def test_find_active_binding_for_branch_only_returns_open(
        self, tmp_path: Path
    ) -> None:
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=1,
            head_branch="feature/x",
            pr_state="open",
        )
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=2,
            head_branch="feature/x",
            pr_state="merged",
        )
        found = store.find_active_binding_for_branch(
            provider="github", repo_slug="acme/test", branch_name="feature/x"
        )
        assert found is not None
        assert found.pr_number == 1

    def test_close_binding_sets_terminal_state(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=3,
            pr_state="open",
        )
        closed = store.close_binding(
            provider="github", repo_slug="acme/test", pr_number=3, pr_state="merged"
        )
        assert closed is not None
        assert closed.pr_state == "merged"
        assert closed.closed_at is not None

    def test_attach_thread_target_overwrites_existing(self, tmp_path: Path) -> None:
        _insert_thread_target(tmp_path, "thread-original")
        _insert_thread_target(tmp_path, "thread-new")
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=5,
            pr_state="open",
            thread_target_id="thread-original",
        )
        result = store.attach_thread_target(
            provider="github",
            repo_slug="acme/test",
            pr_number=5,
            thread_target_id="thread-new",
        )
        assert result is not None
        assert result.thread_target_id == "thread-new"

    def test_upsert_preserves_existing_thread_target(self, tmp_path: Path) -> None:
        _insert_thread_target(tmp_path, "thread-original")
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=5,
            pr_state="open",
            thread_target_id="thread-original",
        )
        updated = store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=5,
            pr_state="open",
        )
        assert updated.thread_target_id == "thread-original"

    def test_attach_thread_target_sets_when_none(self, tmp_path: Path) -> None:
        _insert_thread_target(tmp_path, "thread-new")
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=6,
            pr_state="open",
        )
        result = store.attach_thread_target(
            provider="github",
            repo_slug="acme/test",
            pr_number=6,
            thread_target_id="thread-new",
        )
        assert result is not None
        assert result.thread_target_id == "thread-new"


# ---------------------------------------------------------------------------
# 4. Polling watch store single-owner invariant
# ---------------------------------------------------------------------------


class TestPollingWatchOwnership:
    """ScmPollingWatchStore is the sole owner of orch_scm_polling_watches writes."""

    def _seed_binding(self, hub_root: Path) -> PrBinding:
        store = PrBindingStore(hub_root)
        return store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=10,
            pr_state="open",
            head_branch="feature/watch",
        )

    def test_upsert_watch_creates_active(self, tmp_path: Path) -> None:
        binding = self._seed_binding(tmp_path)
        ws = ScmPollingWatchStore(tmp_path)
        watch = ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws"),
            poll_interval_seconds=300,
            next_poll_at="2026-04-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        assert watch.state == "active"
        assert watch.watch_id is not None

    def test_upsert_watch_is_idempotent_on_provider_binding(
        self, tmp_path: Path
    ) -> None:
        binding = self._seed_binding(tmp_path)
        ws = ScmPollingWatchStore(tmp_path)
        first = ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws"),
            poll_interval_seconds=300,
            next_poll_at="2026-04-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        second = ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws2"),
            poll_interval_seconds=600,
            next_poll_at="2026-04-01T01:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        assert first.watch_id == second.watch_id
        assert second.workspace_root == str(tmp_path / "ws2")
        assert second.poll_interval_seconds == 600

    def test_claim_due_watches_advances_next_poll_at(self, tmp_path: Path) -> None:
        binding = self._seed_binding(tmp_path)
        ws = ScmPollingWatchStore(tmp_path)
        ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws"),
            poll_interval_seconds=300,
            next_poll_at="2026-04-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        first_claim = ws.claim_due_watches(
            provider="github", now_timestamp="2026-04-01T00:05:00Z"
        )
        assert len(first_claim) == 1
        second_claim = ws.claim_due_watches(
            provider="github", now_timestamp="2026-04-01T00:05:00Z"
        )
        assert len(second_claim) == 0

    def test_close_watch_transitions_state(self, tmp_path: Path) -> None:
        binding = self._seed_binding(tmp_path)
        ws = ScmPollingWatchStore(tmp_path)
        watch = ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws"),
            poll_interval_seconds=300,
            next_poll_at="2026-04-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        closed = ws.close_watch(watch_id=watch.watch_id, state="closed")
        assert closed is not None
        assert closed.state == "closed"

    def test_list_due_watches_excludes_non_active(self, tmp_path: Path) -> None:
        binding = self._seed_binding(tmp_path)
        ws = ScmPollingWatchStore(tmp_path)
        watch = ws.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/test",
            pr_number=10,
            workspace_root=str(tmp_path / "ws"),
            poll_interval_seconds=300,
            next_poll_at="2026-04-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        ws.close_watch(watch_id=watch.watch_id, state="expired")
        due = ws.list_due_watches(
            provider="github", now_timestamp="2026-04-01T00:05:00Z"
        )
        assert len(due) == 0


# ---------------------------------------------------------------------------
# 5. Publish journal single-owner invariant
# ---------------------------------------------------------------------------


class TestPublishJournalOwnership:
    """PublishJournalStore is the sole owner of publish operation state."""

    def test_create_operation_dedup_on_active_key(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        first, first_deduped = journal.create_operation(
            operation_key="scm:key-1", operation_kind="notify_chat"
        )
        assert not first_deduped
        assert first.state == "pending"
        second, second_deduped = journal.create_operation(
            operation_key="scm:key-1", operation_kind="notify_chat"
        )
        assert second_deduped
        assert second.operation_id == first.operation_id
        assert _table_row_count(tmp_path, "orch_publish_operations") == 1

    def test_create_operation_allows_new_after_terminal_failure(
        self, tmp_path: Path
    ) -> None:
        journal = PublishJournalStore(tmp_path)
        op, _ = journal.create_operation(
            operation_key="scm:key-term",
            operation_kind="notify_chat",
            next_attempt_at=_PAST_TS,
        )
        journal.claim_pending_operations(now_timestamp=_FAR_FUTURE_TS)
        journal.mark_running(op.operation_id)
        journal.mark_failed(
            op.operation_id, error_text="permanent failure", next_attempt_at=None
        )
        new_op, new_deduped = journal.create_operation(
            operation_key="scm:key-term", operation_kind="notify_chat"
        )
        assert not new_deduped
        assert new_op.operation_id != op.operation_id

    def test_claim_pending_creates_attempt_rows(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        journal.create_operation(
            operation_key="scm:claim-test",
            operation_kind="enqueue_managed_turn",
            next_attempt_at=_PAST_TS,
        )
        claimed = journal.claim_pending_operations(now_timestamp=_FAR_FUTURE_TS)
        assert len(claimed) == 1
        assert claimed[0].state == "running"
        assert claimed[0].attempt_count == 1
        assert _table_row_count(tmp_path, "orch_publish_attempts") == 1

    def test_retry_increments_attempt_count(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        op, _ = journal.create_operation(
            operation_key="scm:retry-test",
            operation_kind="notify_chat",
            next_attempt_at=_PAST_TS,
        )
        first = journal.claim_pending_operations(now_timestamp=_FAR_FUTURE_TS)
        assert len(first) == 1
        journal.mark_running(first[0].operation_id)
        journal.mark_failed(
            first[0].operation_id,
            error_text="temp failure",
            next_attempt_at=_PAST_TS,
        )
        second = journal.claim_pending_operations(now_timestamp=_FAR_FUTURE_TS)
        assert len(second) == 1
        assert second[0].attempt_count == 2

    def test_succeeded_marked_operation_is_deduped(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        op, _ = journal.create_operation(
            operation_key="scm:success-dedup",
            operation_kind="react_pr_review_comment",
            next_attempt_at=_PAST_TS,
        )
        journal.claim_pending_operations(now_timestamp=_FAR_FUTURE_TS)
        journal.mark_running(op.operation_id)
        journal.mark_succeeded(op.operation_id, response={"comment_id": 42})
        duplicate, was_deduped = journal.create_operation(
            operation_key="scm:success-dedup",
            operation_kind="react_pr_review_comment",
        )
        assert was_deduped
        assert duplicate.operation_id == op.operation_id

    def test_priority_ordering_on_claim(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        journal.create_operation(
            operation_key="scm:lo-pri",
            operation_kind="notify_chat",
            next_attempt_at=_PAST_TS,
        )
        journal.create_operation(
            operation_key="scm:hi-pri",
            operation_kind="react_pr_review_comment",
            next_attempt_at=_PAST_TS,
        )
        journal.create_operation(
            operation_key="scm:mid-pri",
            operation_kind="enqueue_managed_turn",
            next_attempt_at=_PAST_TS,
        )
        claimed = journal.claim_pending_operations(
            limit=3, now_timestamp=_FAR_FUTURE_TS
        )
        assert [op.operation_kind for op in claimed] == [
            "react_pr_review_comment",
            "enqueue_managed_turn",
            "notify_chat",
        ]


# ---------------------------------------------------------------------------
# 6. Reaction state store single-owner invariant
# ---------------------------------------------------------------------------


class TestReactionStateOwnership:
    """ScmReactionStateStore is the sole owner of orch_reaction_state."""

    def test_fingerprint_is_deterministic(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        event = _event(event_id="inv:fp-test")
        binding = _binding()
        intent = ReactionIntent(
            reaction_kind="changes_requested",
            operation_kind="enqueue_managed_turn",
            operation_key="scm:key-fp",
            payload={},
            event_id="inv:fp-test",
            binding_id="binding-inv",
        )
        fp1 = store.compute_reaction_fingerprint(event, binding=binding, intent=intent)
        fp2 = store.compute_reaction_fingerprint(event, binding=binding, intent=intent)
        assert fp1 == fp2
        assert len(fp1) > 0

    def test_should_emit_true_when_no_prior_state(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        assert store.should_emit_reaction(
            binding_id="b-1", reaction_kind="r", fingerprint="fp-1"
        )

    def test_should_emit_false_when_emitted(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-2", reaction_kind="r", fingerprint="fp-2"
        )
        assert not store.should_emit_reaction(
            binding_id="b-2", reaction_kind="r", fingerprint="fp-2"
        )

    def test_should_emit_true_when_resolved(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-3", reaction_kind="r", fingerprint="fp-3"
        )
        store.mark_reaction_resolved(
            binding_id="b-3", reaction_kind="r", fingerprint="fp-3"
        )
        assert store.should_emit_reaction(
            binding_id="b-3", reaction_kind="r", fingerprint="fp-3"
        )

    def test_mark_emitted_increments_attempt_count(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-4", reaction_kind="r", fingerprint="fp-4"
        )
        store.mark_reaction_emitted(
            binding_id="b-4", reaction_kind="r", fingerprint="fp-4"
        )
        state = store.get_reaction_state(
            binding_id="b-4", reaction_kind="r", fingerprint="fp-4"
        )
        assert state is not None
        assert state.attempt_count == 2

    def test_delivery_failed_increments_failure_count(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-5", reaction_kind="r", fingerprint="fp-5"
        )
        store.mark_reaction_delivery_failed(
            binding_id="b-5",
            reaction_kind="r",
            fingerprint="fp-5",
            error_text="timeout",
        )
        state = store.get_reaction_state(
            binding_id="b-5", reaction_kind="r", fingerprint="fp-5"
        )
        assert state is not None
        assert state.delivery_failure_count == 1
        assert state.state == "delivery_failed"

    def test_escalation_sets_escalated_at_once(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-6", reaction_kind="r", fingerprint="fp-6"
        )
        store.mark_reaction_escalated(
            binding_id="b-6", reaction_kind="r", fingerprint="fp-6"
        )
        first = store.get_reaction_state(
            binding_id="b-6", reaction_kind="r", fingerprint="fp-6"
        )
        assert first is not None and first.escalated_at is not None
        first_escalated_at = first.escalated_at
        store.mark_reaction_escalated(
            binding_id="b-6", reaction_kind="r", fingerprint="fp-6"
        )
        second = store.get_reaction_state(
            binding_id="b-6", reaction_kind="r", fingerprint="fp-6"
        )
        assert second is not None
        assert second.escalated_at == first_escalated_at

    def test_resolve_other_active_reactions(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="b-7", reaction_kind="r", fingerprint="fp-old"
        )
        store.mark_reaction_emitted(
            binding_id="b-7", reaction_kind="r", fingerprint="fp-new"
        )
        resolved_count = store.resolve_other_active_reactions(
            binding_id="b-7",
            reaction_kind="r",
            keep_fingerprint="fp-new",
        )
        assert resolved_count == 1
        old_state = store.get_reaction_state(
            binding_id="b-7", reaction_kind="r", fingerprint="fp-old"
        )
        assert old_state is not None and old_state.state == "resolved"
        new_state = store.get_reaction_state(
            binding_id="b-7", reaction_kind="r", fingerprint="fp-new"
        )
        assert new_state is not None and new_state.state == "emitted"


# ---------------------------------------------------------------------------
# 7. Cross-store automation pipeline invariant
# ---------------------------------------------------------------------------


class TestAutomationPipelineInvariants:
    """Freeze that ScmAutomationService writes to the correct stores in order."""

    def _record_event_from_scm(self, store: ScmEventStore, event: ScmEvent) -> None:
        d = event.to_dict()
        d.pop("created_at", None)
        d.pop("updated_at", None)
        store.record_event(**d)

    def test_ingest_event_writes_reaction_state_and_journal_only(
        self, tmp_path: Path
    ) -> None:
        event = _event(event_id="inv:pipeline-1")
        binding = _binding()
        store = ScmEventStore(tmp_path)
        self._record_event_from_scm(store, event)

        reaction_state = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=store,
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=reaction_state,
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        result = service.ingest_event("inv:pipeline-1")

        assert len(result.publish_operations) >= 1

        reaction_states = reaction_state.list_reaction_states(binding_id="binding-inv")
        assert len(reaction_states) >= 1

        ops = journal.list_operations()
        assert len(ops) >= 1

    def test_process_now_delegates_to_processor_and_updates_reaction_state(
        self, tmp_path: Path
    ) -> None:
        event = _event(event_id="inv:process-1")
        binding = _binding()
        store = ScmEventStore(tmp_path)
        self._record_event_from_scm(store, event)

        reaction_state = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=store,
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=reaction_state,
            journal=journal,
            publish_processor=_ProcessorFake(),
        )
        ingested = service.ingest_event("inv:process-1")
        assert len(ingested.publish_operations) >= 1

    def test_duplicate_event_suppression_does_not_create_journal_entry(
        self, tmp_path: Path
    ) -> None:
        first_event = _event(event_id="inv:dup-1")
        second_event = _event(event_id="inv:dup-2")
        binding = _binding()
        store = ScmEventStore(tmp_path)
        self._record_event_from_scm(store, first_event)
        self._record_event_from_scm(store, second_event)

        reaction_state = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=store,
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=reaction_state,
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        first = service.ingest_event("inv:dup-1")
        second = service.ingest_event("inv:dup-2")

        assert len(first.publish_operations) == 1
        assert len(second.publish_operations) == 0
        ops = journal.list_operations()
        assert len(ops) == 1

        state = reaction_state.list_reaction_states(binding_id="binding-inv")
        assert any(s.attempt_count == 2 for s in state)

    def test_condition_change_resolves_old_and_emits_new(self, tmp_path: Path) -> None:
        first_event = _event(
            event_id="inv:cond-1",
            payload={
                "action": "submitted",
                "review_state": "changes_requested",
                "body": "old condition",
            },
        )
        changed_event = _event(
            event_id="inv:cond-2",
            payload={
                "action": "submitted",
                "review_state": "changes_requested",
                "body": "new condition",
            },
        )
        binding = _binding()
        store = ScmEventStore(tmp_path)
        self._record_event_from_scm(store, first_event)
        self._record_event_from_scm(store, changed_event)

        reaction_state = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=store,
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=reaction_state,
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        first = service.ingest_event("inv:cond-1")
        second = service.ingest_event("inv:cond-2")

        assert len(first.publish_operations) == 1
        assert len(second.publish_operations) == 1
        ops = journal.list_operations()
        assert len(ops) == 2

    def test_no_binding_still_emits_notify_chat_fallback(self, tmp_path: Path) -> None:
        event = _event(event_id="inv:no-binding")
        store = ScmEventStore(tmp_path)
        self._record_event_from_scm(store, event)

        service = ScmAutomationService(
            tmp_path,
            event_store=store,
            binding_resolver=_BindingResolverFake(None),
            reaction_router=route_scm_reactions,
            reaction_state_store=_PermissiveReactionStateFake(),
            journal=_JournalFake(),
            publish_processor=_ProcessorFake(),
        )

        result = service.ingest_event("inv:no-binding")
        assert result.binding is None
        assert len(result.publish_operations) == 1
        assert result.publish_operations[0].operation_kind == "notify_chat"
        assert result.publish_operations[0].payload.get("binding_id") is None


# ---------------------------------------------------------------------------
# 8. Self-claim adapter ownership invariant
# ---------------------------------------------------------------------------


class TestSelfClaimOwnership:
    """managed_thread_pr_binding writes to pr_bindings and polling_watches only."""

    def test_self_claim_does_not_steal_existing_binding(self, tmp_path: Path) -> None:
        _insert_thread_target(tmp_path, "thread-other")
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=20,
            pr_state="open",
            head_branch="feature/claimed",
            thread_target_id="thread-other",
        )
        from codex_autorunner.core.pr_binding_runtime import (
            claim_pr_binding_for_thread,
        )

        result = claim_pr_binding_for_thread(
            tmp_path,
            provider="github",
            repo_slug="acme/test",
            pr_number=20,
            pr_state="open",
            thread_target_id="thread-self",
        )
        assert result is not None
        assert result.thread_target_id == "thread-other"

    def test_self_claim_attaches_when_unbound(self, tmp_path: Path) -> None:
        _insert_thread_target(tmp_path, "thread-self")
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/test",
            pr_number=21,
            pr_state="open",
            head_branch="feature/unbound",
        )
        from codex_autorunner.core.pr_binding_runtime import (
            claim_pr_binding_for_thread,
        )

        result = claim_pr_binding_for_thread(
            tmp_path,
            provider="github",
            repo_slug="acme/test",
            pr_number=21,
            pr_state="open",
            thread_target_id="thread-self",
        )
        assert result is not None
        assert result.thread_target_id == "thread-self"
