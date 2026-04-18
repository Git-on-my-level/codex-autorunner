from __future__ import annotations

from pathlib import Path
from typing import Optional

from codex_autorunner.core.pr_bindings import PrBinding, PrBindingStore
from codex_autorunner.core.publish_journal import PublishJournalStore
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEvent, ScmEventStore
from codex_autorunner.core.scm_polling_watches import ScmPollingWatchStore
from codex_autorunner.core.scm_reaction_router import route_scm_reactions
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore


def _event(
    *,
    event_id: str = "github:seam-1",
    event_type: str = "pull_request_review",
    pr_number: int = 42,
    repo_slug: str = "acme/widgets",
    repo_id: str = "repo-1",
    payload: dict | None = None,
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type=event_type,
        occurred_at="2026-04-15T00:00:00Z",
        received_at="2026-04-15T00:00:01Z",
        created_at="2026-04-15T00:00:02Z",
        repo_slug=repo_slug,
        repo_id=repo_id,
        pr_number=pr_number,
        delivery_id="delivery-seam",
        payload=payload or {"action": "submitted", "review_state": "changes_requested"},
        raw_payload=None,
    )


def _binding(
    *,
    binding_id: str = "binding-seam",
    pr_number: int = 42,
    repo_slug: str = "acme/widgets",
    thread_target_id: str | None = "thread-seam",
) -> PrBinding:
    return PrBinding(
        binding_id=binding_id,
        provider="github",
        repo_slug=repo_slug,
        repo_id="repo-1",
        pr_number=pr_number,
        pr_state="open",
        head_branch="feature/seam-test",
        base_branch="main",
        thread_target_id=thread_target_id,
        created_at="2026-04-15T00:00:00Z",
        updated_at="2026-04-15T00:00:00Z",
        closed_at=None,
    )


class _BindingResolverFake:
    def __init__(self, binding: Optional[PrBinding]) -> None:
        self.binding = binding

    def __call__(
        self,
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]:
        return self.binding


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
        _ = next_attempt_at
        if operation_key in self.operations_by_key:
            return self.operations_by_key[operation_key], True
        return object(), False


class _ProcessorFake:
    def __init__(self) -> None:
        self.results: list[object] = []

    def process_now(self, limit: int = 10) -> list[object]:
        return list(self.results)


class TestWebhookEventToBindingResolution:
    def test_persisted_event_resolves_to_binding_and_creates_publish_operations(
        self, tmp_path: Path
    ) -> None:
        event_store = ScmEventStore(tmp_path)
        event = event_store.record_event(
            event_id="github:seam-persist-1",
            provider="github",
            event_type="pull_request_review",
            repo_slug="acme/widgets",
            repo_id="repo-1",
            pr_number=42,
            payload={
                "action": "submitted",
                "review_state": "changes_requested",
                "author_login": "reviewer",
                "body": "Cross-seam test.",
            },
        )
        binding = _binding()
        journal = PublishJournalStore(tmp_path)
        processor = _ProcessorFake()
        service = ScmAutomationService(
            tmp_path,
            event_store=event_store,
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=ScmReactionStateStore(tmp_path),
            journal=journal,
            publish_processor=processor,
        )

        result = service.ingest_event(event)

        assert result.binding is not None
        assert result.binding.binding_id == "binding-seam"
        assert len(result.publish_operations) >= 1
        first_op = result.publish_operations[0]
        assert first_op.operation_kind == "enqueue_managed_turn"
        tracking = first_op.payload.get("scm_reaction", {})
        assert tracking["binding_id"] == "binding-seam"
        assert tracking["event_id"] == "github:seam-persist-1"

        loaded = event_store.get_event("github:seam-persist-1")
        assert loaded is not None
        assert loaded.payload["review_state"] == "changes_requested"


class TestPollingWatchArmedAfterBindingUpsert:
    def test_binding_upsert_then_watch_arm_produces_consistent_state(
        self, tmp_path: Path
    ) -> None:
        store = PrBindingStore(tmp_path)
        binding = store.upsert_binding(
            provider="github",
            repo_slug="acme/widgets",
            repo_id="repo-1",
            pr_number=55,
            pr_state="open",
            head_branch="feature/watch-seam",
            base_branch="main",
        )
        assert binding.thread_target_id is None

        watch_store = ScmPollingWatchStore(tmp_path)
        watch = watch_store.upsert_watch(
            provider="github",
            binding_id=binding.binding_id,
            repo_slug="acme/widgets",
            repo_id="repo-1",
            pr_number=55,
            workspace_root=str(tmp_path / "repo-55"),
            poll_interval_seconds=90,
            next_poll_at="2026-04-15T01:00:00Z",
            expires_at="2026-04-15T02:00:00Z",
            reaction_config={"enabled": True},
            snapshot={"head_sha": "sha55"},
        )
        assert watch.binding_id == binding.binding_id
        assert watch.state == "active"

        refreshed_binding = store.get_binding_by_pr(
            provider="github",
            repo_slug="acme/widgets",
            pr_number=55,
        )
        assert refreshed_binding is not None
        assert refreshed_binding.binding_id == binding.binding_id

        refreshed_watch = watch_store.get_watch(
            provider="github",
            binding_id=binding.binding_id,
        )
        assert refreshed_watch is not None
        assert refreshed_watch.snapshot == {"head_sha": "sha55"}


class TestPublishDedupeAcrossAutomatedIngest:
    def test_duplicate_ingest_does_not_create_duplicate_journal_entries(
        self, tmp_path: Path
    ) -> None:
        event = _event(event_id="github:seam-dedup-1")
        binding = _binding()
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=ScmEventStore(tmp_path),
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=ScmReactionStateStore(tmp_path),
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        first = service.ingest_event(event)
        second = service.ingest_event(event)

        assert len(first.publish_operations) >= 1
        assert second.publish_operations == ()
        first_keys = {op.operation_key for op in first.publish_operations}
        assert len(first_keys) == len(first.publish_operations)


class TestReactionStateTracksAcrossSeams:
    def test_emitted_reaction_prevents_duplicate_after_separate_ingest(
        self, tmp_path: Path
    ) -> None:
        first_event = _event(event_id="github:seam-state-1")
        second_event = _event(event_id="github:seam-state-2")
        binding = _binding()
        state_store = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=ScmEventStore(tmp_path),
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=state_store,
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        first_result = service.ingest_event(first_event)
        second_result = service.ingest_event(second_event)

        assert len(first_result.publish_operations) >= 1
        assert second_result.publish_operations == ()

        intent = first_result.reaction_intents[0]
        fingerprint = state_store.compute_reaction_fingerprint(
            first_event,
            binding=binding,
            intent=intent,
        )
        state = state_store.get_reaction_state(
            binding_id="binding-seam",
            reaction_kind="changes_requested",
            fingerprint=fingerprint,
        )
        assert state is not None
        assert state.state == "emitted"
        assert state.attempt_count == 2

    def test_delivery_failure_updates_reaction_state_across_publish_cycle(
        self, tmp_path: Path
    ) -> None:
        event = _event(event_id="github:seam-del-fail")
        binding = _binding()
        state_store = ScmReactionStateStore(tmp_path)
        journal = PublishJournalStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=ScmEventStore(tmp_path),
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=state_store,
            reaction_config={"delivery_failure_escalation_threshold": 2},
            journal=journal,
            publish_processor=_ProcessorFake(),
        )

        result = service.ingest_event(event)
        assert len(result.publish_operations) >= 1
        original_op = result.publish_operations[0]
        tracking = original_op.payload["scm_reaction"]

        claimed = journal.claim_pending_operations(limit=10)
        assert len(claimed) == 1
        failed_op = journal.mark_failed(claimed[0].operation_id, error_text="timeout")
        assert failed_op is not None
        assert failed_op.state == "failed"

        service._handle_processed_operations([failed_op])

        state = state_store.get_reaction_state(
            binding_id=tracking["binding_id"],
            reaction_kind=tracking["reaction_kind"],
            fingerprint=tracking["fingerprint"],
        )
        assert state is not None
        assert state.delivery_failure_count == 1

    def test_resolved_reaction_allows_reemit_on_condition_change(
        self, tmp_path: Path
    ) -> None:
        first_event = _event(
            event_id="github:seam-resolve-1",
            payload={
                "action": "submitted",
                "review_state": "changes_requested",
                "author_login": "reviewer",
                "body": "First review.",
            },
        )
        changed_event = _event(
            event_id="github:seam-resolve-2",
            payload={
                "action": "submitted",
                "review_state": "changes_requested",
                "author_login": "reviewer",
                "body": "Different review body.",
            },
        )
        binding = _binding()
        state_store = ScmReactionStateStore(tmp_path)
        service = ScmAutomationService(
            tmp_path,
            event_store=ScmEventStore(tmp_path),
            binding_resolver=_BindingResolverFake(binding),
            reaction_router=route_scm_reactions,
            reaction_state_store=state_store,
            journal=PublishJournalStore(tmp_path),
            publish_processor=_ProcessorFake(),
        )

        first_result = service.ingest_event(first_event)
        changed_result = service.ingest_event(changed_event)

        assert len(first_result.publish_operations) >= 1
        assert len(changed_result.publish_operations) >= 1

        first_fp = state_store.compute_reaction_fingerprint(
            first_event,
            binding=binding,
            intent=first_result.reaction_intents[0],
        )
        changed_fp = state_store.compute_reaction_fingerprint(
            changed_event,
            binding=binding,
            intent=changed_result.reaction_intents[0],
        )
        first_state = state_store.get_reaction_state(
            binding_id="binding-seam",
            reaction_kind="changes_requested",
            fingerprint=first_fp,
        )
        changed_state = state_store.get_reaction_state(
            binding_id="binding-seam",
            reaction_kind="changes_requested",
            fingerprint=changed_fp,
        )
        assert first_state is not None
        assert first_state.state == "resolved"
        assert first_state.resolved_at is not None
        assert changed_state is not None
        assert changed_state.state == "emitted"
