from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_autorunner.core.pr_bindings import PrBindingStore
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.scm_events import ScmEventStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore

_REQUIRED_EVENT_DICT_KEYS = frozenset(
    {
        "event_id",
        "provider",
        "event_type",
        "occurred_at",
        "received_at",
        "created_at",
        "repo_slug",
        "repo_id",
        "pr_number",
        "delivery_id",
        "correlation_id",
        "payload",
        "raw_payload",
    }
)


class TestScmEventShape:
    def test_event_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        event = ScmEventStore(tmp_path).record_event(
            event_id="github:shape-1",
            provider="github",
            event_type="pull_request",
            repo_slug="acme/widgets",
            repo_id="99",
            pr_number=42,
            delivery_id="delivery-1",
            correlation_id="scm:github:delivery-1",
            payload={"action": "opened"},
        )
        d = event.to_dict()
        assert _REQUIRED_EVENT_DICT_KEYS <= frozenset(d)
        assert d["event_id"] == "github:shape-1"
        assert d["provider"] == "github"
        assert d["event_type"] == "pull_request"
        assert d["pr_number"] == 42
        assert d["repo_slug"] == "acme/widgets"
        assert d["repo_id"] == "99"
        assert d["delivery_id"] == "delivery-1"
        assert d["correlation_id"] == "scm:github:delivery-1"
        assert isinstance(d["payload"], dict)
        assert d["raw_payload"] is None

    def test_event_to_dict_preserves_raw_payload(self, tmp_path: Path) -> None:
        raw = {"action": "opened", "extra": True}
        event = ScmEventStore(tmp_path).record_event(
            event_id="github:shape-raw",
            provider="github",
            event_type="pull_request",
            payload={"action": "opened"},
            raw_payload=raw,
        )
        d = event.to_dict()
        assert d["raw_payload"] == raw
        assert d["payload"] == {"action": "opened"}

    def test_event_persistence_roundtrip(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        original = store.record_event(
            event_id="github:roundtrip-1",
            provider="github",
            event_type="pull_request_review",
            occurred_at="2026-04-01T10:00:00Z",
            received_at="2026-04-01T10:00:01Z",
            repo_slug="acme/widgets",
            repo_id="repo-1",
            pr_number=17,
            delivery_id="delivery-rt",
            correlation_id="corr-rt",
            payload={"action": "submitted", "review_state": "approved"},
        )
        loaded = store.get_event("github:roundtrip-1")
        assert loaded is not None
        assert loaded.to_dict() == original.to_dict()

    def test_event_dedup_rejects_duplicate_event_id(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        store.record_event(
            event_id="github:dedup-1",
            provider="github",
            event_type="pull_request",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_event(
                event_id="github:dedup-1",
                provider="github",
                event_type="pull_request",
            )

    def test_event_list_filters_by_provider_and_repo(self, tmp_path: Path) -> None:
        store = ScmEventStore(tmp_path)
        store.record_event(
            event_id="github:filter-1",
            provider="github",
            event_type="pull_request",
            repo_slug="acme/alpha",
        )
        store.record_event(
            event_id="github:filter-2",
            provider="github",
            event_type="push",
            repo_slug="acme/beta",
        )
        alpha = store.list_events(provider="github", repo_slug="acme/alpha")
        assert len(alpha) == 1
        assert alpha[0].event_id == "github:filter-1"
        beta = store.list_events(provider="github", repo_slug="acme/beta")
        assert len(beta) == 1
        assert beta[0].event_id == "github:filter-2"

    def test_event_optional_fields_default_to_none(self, tmp_path: Path) -> None:
        event = ScmEventStore(tmp_path).record_event(
            event_id="github:minimal-1",
            provider="github",
            event_type="push",
        )
        assert event.repo_slug is None
        assert event.repo_id is None
        assert event.pr_number is None
        assert event.delivery_id is None
        assert event.correlation_id is None
        assert event.raw_payload is None


_REQUIRED_BINDING_DICT_KEYS = frozenset(
    {
        "binding_id",
        "provider",
        "repo_slug",
        "repo_id",
        "pr_number",
        "pr_state",
        "head_branch",
        "base_branch",
        "thread_target_id",
        "created_at",
        "updated_at",
        "closed_at",
    }
)


class TestPrBindingShape:
    def test_binding_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        binding = PrBindingStore(tmp_path).upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            repo_id="repo-1",
            pr_number=42,
            pr_state="open",
            head_branch="feature/test",
            base_branch="main",
        )
        d = binding.to_dict()
        assert _REQUIRED_BINDING_DICT_KEYS <= frozenset(d)
        assert d["provider"] == "github"
        assert d["repo_slug"] == "acme/widgets"
        assert d["pr_number"] == 42
        assert d["pr_state"] == "open"
        assert d["head_branch"] == "feature/test"
        assert d["base_branch"] == "main"
        assert d["closed_at"] is None

    def test_binding_upsert_is_idempotent(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        first = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
            head_branch="feature/idem",
        )
        second = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
            head_branch="feature/idem",
        )
        assert first.binding_id == second.binding_id

    def test_binding_state_transitions_set_closed_at(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        open_binding = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
        )
        assert open_binding.closed_at is None
        merged_binding = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="merged",
        )
        assert merged_binding.closed_at is not None
        assert merged_binding.pr_state == "merged"

    def test_binding_lookup_by_pr_identity(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
        )
        found = store.get_binding_by_pr(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
        )
        assert found is not None
        assert found.pr_number == 42
        missing = store.get_binding_by_pr(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=99,
        )
        assert missing is None

    def test_binding_upsert_coalesces_repo_id(self, tmp_path: Path) -> None:
        store = PrBindingStore(tmp_path)
        first = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
            head_branch="feature/coalesce",
        )
        assert first.repo_id is None
        second = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=42,
            pr_state="open",
            repo_id="repo-coalesced",
        )
        assert second.binding_id == first.binding_id
        assert second.repo_id == "repo-coalesced"


_REQUIRED_WATCH_DICT_KEYS = frozenset(
    {
        "watch_id",
        "provider",
        "binding_id",
        "repo_slug",
        "repo_id",
        "pr_number",
        "workspace_root",
        "poll_interval_seconds",
        "state",
        "started_at",
        "updated_at",
        "expires_at",
        "next_poll_at",
        "thread_target_id",
        "last_polled_at",
        "last_error_text",
        "reaction_config",
        "snapshot",
    }
)


class TestScmPollingWatchShape:
    def test_watch_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        binding = PrBindingStore(tmp_path).upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=17,
            pr_state="open",
        )
        watch = ScmPollingWatchStore(tmp_path).upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/widgets",
            pr_number=17,
            workspace_root=str(tmp_path / "repo"),
            poll_interval_seconds=90,
            next_poll_at="2026-04-01T10:01:30Z",
            expires_at="2026-04-01T11:00:00Z",
            reaction_config={"enabled": True},
            snapshot={"head_sha": "abc123"},
        )
        d = watch.to_dict()
        assert _REQUIRED_WATCH_DICT_KEYS <= frozenset(d)
        assert d["provider"] == "github"
        assert d["state"] == "active"
        assert d["reaction_config"] == {"enabled": True}
        assert d["snapshot"] == {"head_sha": "abc123"}

    def test_watch_close_sets_state_to_closed(self, tmp_path: Path) -> None:
        store = ScmPollingWatchStore(tmp_path)
        binding = PrBindingStore(tmp_path).upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=17,
            pr_state="open",
        )
        watch = store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/widgets",
            pr_number=17,
            workspace_root=str(tmp_path / "repo"),
            poll_interval_seconds=90,
            next_poll_at="2026-04-01T10:01:30Z",
            expires_at="2026-04-01T11:00:00Z",
        )
        assert watch.state == "active"
        store.close_watch(watch_id=watch.watch_id, state="closed")
        closed = store.get_watch(provider="github", binding_id=binding.binding_id)
        assert closed is not None
        assert closed.state == "closed"

    def test_watch_upsert_reactivates_closed_watch(self, tmp_path: Path) -> None:
        store = ScmPollingWatchStore(tmp_path)
        binding = PrBindingStore(tmp_path).upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=17,
            pr_state="open",
        )
        first = store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/widgets",
            pr_number=17,
            workspace_root=str(tmp_path / "repo"),
            poll_interval_seconds=90,
            next_poll_at="2026-04-01T10:01:30Z",
            expires_at="2026-04-01T11:00:00Z",
        )
        store.close_watch(watch_id=first.watch_id, state="closed")
        second = store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/widgets",
            pr_number=17,
            workspace_root=str(tmp_path / "repo"),
            poll_interval_seconds=60,
            next_poll_at="2026-04-01T10:02:00Z",
            expires_at="2026-04-01T12:00:00Z",
        )
        assert second.watch_id == first.watch_id
        assert second.state == "active"
        assert second.poll_interval_seconds == 60


_REQUIRED_OPERATION_DICT_KEYS = frozenset(
    {
        "operation_id",
        "operation_key",
        "operation_kind",
        "state",
        "payload",
        "response",
        "created_at",
        "updated_at",
        "claimed_at",
        "started_at",
        "finished_at",
        "next_attempt_at",
        "last_error_text",
        "attempt_count",
    }
)


class TestPublishOperationShape:
    def test_operation_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        operation, deduped = journal.create_operation(
            operation_key="github:comment:shape-test",
            operation_kind="github_comment",
            payload={"body": "test"},
        )
        assert deduped is False
        d = operation.to_dict()
        assert _REQUIRED_OPERATION_DICT_KEYS <= frozenset(d)
        assert d["state"] == "pending"
        assert d["operation_kind"] == "github_comment"
        assert d["payload"] == {"body": "test"}
        assert d["attempt_count"] == 0

    def test_operation_dedup_returns_same_key(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        first, first_deduped = journal.create_operation(
            operation_key="github:comment:dedup-test",
            operation_kind="github_comment",
        )
        second, second_deduped = journal.create_operation(
            operation_key="github:comment:dedup-test",
            operation_kind="github_comment",
        )
        assert first_deduped is False
        assert second_deduped is True
        assert first.operation_id == second.operation_id

    def test_claim_pending_advances_state_to_running(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        op, _ = journal.create_operation(
            operation_key="github:comment:claim-test",
            operation_kind="github_comment",
        )
        assert op.state == "pending"
        claimed = journal.claim_pending_operations(limit=10)
        assert len(claimed) == 1
        assert claimed[0].operation_id == op.operation_id
        assert claimed[0].state == "running"
        assert claimed[0].claimed_at is not None

    def test_mark_succeeded_finalizes_operation(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        journal.create_operation(
            operation_key="github:comment:succeed-test",
            operation_kind="github_comment",
        )
        claimed = journal.claim_pending_operations(limit=10)
        assert len(claimed) == 1
        succeeded = journal.mark_succeeded(
            claimed[0].operation_id,
            response={"delivered": True},
        )
        assert succeeded is not None
        assert succeeded.state == "succeeded"
        assert succeeded.response == {"delivered": True}
        assert succeeded.finished_at is not None

    def test_mark_failed_records_error_after_claim(self, tmp_path: Path) -> None:
        journal = PublishJournalStore(tmp_path)
        journal.create_operation(
            operation_key="github:comment:fail-test",
            operation_kind="github_comment",
        )
        claimed = journal.claim_pending_operations(limit=10)
        assert len(claimed) == 1
        failed = journal.mark_failed(
            claimed[0].operation_id,
            error_text="timeout",
        )
        assert failed is not None
        assert failed.state == "failed"
        assert failed.last_error_text == "timeout"
        assert failed.attempt_count == 1


class TestScmReactionStateShape:
    def test_reaction_state_emitted_roundtrip(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="binding-1",
            reaction_kind="ci_failed",
            fingerprint="fp-1",
            event_id="github:event-1",
            operation_key="scm:key-1",
        )
        state = store.get_reaction_state(
            binding_id="binding-1",
            reaction_kind="ci_failed",
            fingerprint="fp-1",
        )
        assert state is not None
        assert state.state == "emitted"
        assert state.attempt_count == 1
        assert state.last_event_id == "github:event-1"
        assert state.last_operation_key == "scm:key-1"

    def test_reaction_state_resolved_allows_reemit(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-resolve",
            event_id="github:event-1",
        )
        assert not store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-resolve",
        )
        store.mark_reaction_resolved(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-resolve",
            event_id="github:event-2",
        )
        state = store.get_reaction_state(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-resolve",
        )
        assert state is not None
        assert state.state == "resolved"
        assert state.resolved_at is not None
        assert store.should_emit_reaction(
            binding_id="binding-1",
            reaction_kind="changes_requested",
            fingerprint="fp-resolve",
        )

    def test_reaction_state_delivery_failure_tracks_count(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        result = store.mark_reaction_delivery_failed(
            binding_id="binding-1",
            reaction_kind="ci_failed",
            fingerprint="fp-del-fail",
            event_id="github:event-del-1",
            error_text="timeout",
        )
        assert result.delivery_failure_count == 1
        assert result.escalated_at is None

    def test_reaction_state_escalated_sets_timestamp(self, tmp_path: Path) -> None:
        store = ScmReactionStateStore(tmp_path)
        store.mark_reaction_emitted(
            binding_id="binding-1",
            reaction_kind="ci_failed",
            fingerprint="fp-esc-1",
            event_id="github:event-esc-1",
        )
        result = store.mark_reaction_escalated(
            binding_id="binding-1",
            reaction_kind="ci_failed",
            fingerprint="fp-esc-1",
            event_id="github:event-esc-2",
            operation_key="scm:esc-key",
        )
        assert result.escalated_at is not None

    def test_reaction_state_kind_namespaces_review_comments(self) -> None:
        from codex_autorunner.core.scm_reaction_state import reaction_state_kind

        assert (
            reaction_state_kind(
                reaction_kind="changes_requested", operation_kind="enqueue_managed_turn"
            )
            == "changes_requested"
        )
        assert (
            reaction_state_kind(
                reaction_kind="review_comment", operation_kind="react_pr_review_comment"
            )
            == "review_comment:react_pr_review_comment"
        )
        assert (
            reaction_state_kind(
                reaction_kind="review_comment", operation_kind="enqueue_managed_turn"
            )
            == "review_comment:enqueue_managed_turn"
        )
