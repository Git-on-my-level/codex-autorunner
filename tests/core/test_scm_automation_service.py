from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from codex_autorunner.core.pr_bindings import PrBinding
from codex_autorunner.core.publish_journal import PublishOperation
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEvent
from codex_autorunner.core.scm_reaction_router import route_scm_reactions
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore
from codex_autorunner.core.scm_reaction_types import ReactionIntent, ScmReactionConfig


def _event(
    *,
    event_id: str = "github:event-1",
    payload: dict[str, object] | None = None,
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type="pull_request_review",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload=payload or {"action": "submitted", "review_state": "changes_requested"},
        raw_payload=None,
    )


def _binding() -> PrBinding:
    return PrBinding(
        binding_id="binding-1",
        provider="github",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        pr_state="open",
        head_branch="feature/scm-automation",
        base_branch="main",
        thread_target_id="thread-123",
        created_at="2026-03-25T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
        closed_at=None,
    )


def _intent(
    *,
    operation_key: str,
    operation_kind: str = "enqueue_managed_turn",
    event_id: str = "github:event-1",
) -> ReactionIntent:
    return ReactionIntent(
        reaction_kind="changes_requested",
        operation_kind=operation_kind,
        operation_key=operation_key,
        payload={"operation_key": operation_key},
        event_id=event_id,
        binding_id="binding-1",
    )


def _operation(
    *,
    operation_id: str,
    operation_key: str,
    operation_kind: str,
    state: str = "pending",
) -> PublishOperation:
    return PublishOperation(
        operation_id=operation_id,
        operation_key=operation_key,
        operation_kind=operation_kind,
        state=state,
        payload={"operation_key": operation_key},
        response={},
        created_at="2026-03-26T00:00:10Z",
        updated_at="2026-03-26T00:00:10Z",
        claimed_at=None,
        started_at=None,
        finished_at=None,
        next_attempt_at="2026-03-26T00:00:10Z",
        last_error_text=None,
        attempt_count=0,
    )


class _EventStoreFake:
    def __init__(self, *events: ScmEvent) -> None:
        self._events = {event.event_id: event for event in events}
        self.lookups: list[str] = []

    def get_event(self, event_id: str) -> Optional[ScmEvent]:
        self.lookups.append(event_id)
        return self._events.get(event_id)


class _BindingResolverFake:
    def __init__(self, binding: Optional[PrBinding]) -> None:
        self.binding = binding
        self.calls: list[tuple[str, Optional[str]]] = []

    def __call__(
        self,
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]:
        self.calls.append((event.event_id, thread_target_id))
        return self.binding


class _ReactionRouterFake:
    def __init__(self, intents: list[ReactionIntent]) -> None:
        self.intents = intents
        self.calls: list[tuple[str, Optional[str], ScmReactionConfig]] = []

    def __call__(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding] = None,
        config: ScmReactionConfig | dict | None = None,
    ) -> list[ReactionIntent]:
        resolved_config = ScmReactionConfig.from_mapping(config)
        self.calls.append(
            (
                event.event_id,
                binding.binding_id if binding is not None else None,
                resolved_config,
            )
        )
        return list(self.intents)


class _JournalFake:
    def __init__(self) -> None:
        self.operations_by_key: dict[str, PublishOperation] = {}
        self.create_calls: list[tuple[str, str]] = []

    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]:
        _ = next_attempt_at
        self.create_calls.append((operation_key, operation_kind))
        existing = self.operations_by_key.get(operation_key)
        if existing is not None:
            return existing, True
        created = _operation(
            operation_id=f"op-{len(self.operations_by_key) + 1}",
            operation_key=operation_key,
            operation_kind=operation_kind,
        )
        created = PublishOperation(
            **{**created.to_dict(), "payload": dict(payload or {})}
        )
        self.operations_by_key[operation_key] = created
        return created, False


class _ProcessorFake:
    def __init__(self, processed: list[PublishOperation]) -> None:
        self.processed = processed
        self.calls: list[int] = []

    def process_now(self, limit: int = 10) -> list[PublishOperation]:
        self.calls.append(limit)
        return list(self.processed)


class _PermissiveReactionStateFake:
    def __init__(self) -> None:
        self.should_calls: list[tuple[str, str, str]] = []
        self.mark_calls: list[tuple[str, str, str, Optional[str], Optional[str]]] = []

    def compute_reaction_fingerprint(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding],
        intent: ReactionIntent,
    ) -> str:
        _ = binding
        return f"{intent.reaction_kind}:{event.event_id}:{intent.operation_kind}"

    def should_emit_reaction(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
    ) -> bool:
        self.should_calls.append((binding_id, reaction_kind, fingerprint))
        return True

    def mark_reaction_emitted(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> object:
        _ = metadata
        self.mark_calls.append(
            (binding_id, reaction_kind, fingerprint, event_id, operation_key)
        )
        return object()

    def get_reaction_state(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
    ) -> object | None:
        _ = binding_id, reaction_kind, fingerprint
        return None

    def mark_reaction_suppressed(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> object:
        _ = binding_id, reaction_kind, fingerprint, event_id, metadata
        return object()

    def mark_reaction_escalated(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> object:
        _ = binding_id, reaction_kind, fingerprint, event_id, operation_key, metadata
        return object()

    def mark_reaction_delivery_failed(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        error_text: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> object:
        _ = binding_id, reaction_kind, fingerprint, event_id, error_text, metadata
        return SimpleNamespace(escalated_at=None, delivery_failure_count=1)

    def mark_reaction_delivery_succeeded(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> object:
        _ = binding_id, reaction_kind, fingerprint, event_id, operation_key, metadata
        return object()

    def resolve_other_active_reactions(
        self,
        *,
        binding_id: str,
        reaction_kind: str,
        keep_fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        _ = binding_id, reaction_kind, keep_fingerprint, event_id, metadata
        return 0


class _ProcessorSequenceFake:
    def __init__(self, *batches: list[PublishOperation]) -> None:
        self._batches = list(batches)
        self.calls: list[int] = []

    def process_now(self, limit: int = 10) -> list[PublishOperation]:
        self.calls.append(limit)
        if not self._batches:
            return []
        return list(self._batches.pop(0))


def test_ingest_event_loads_persisted_event_routes_reactions_and_dedupes_publish_operations(
    tmp_path: Path,
) -> None:
    event = _event()
    binding = _binding()
    event_store = _EventStoreFake(event)
    binding_resolver = _BindingResolverFake(binding)
    reaction_router = _ReactionRouterFake(
        [
            _intent(operation_key="scm:key-1"),
            _intent(operation_key="scm:key-1"),
            _intent(operation_key="scm:key-2", operation_kind="notify_chat"),
        ]
    )
    journal = _JournalFake()
    processor = _ProcessorFake(processed=[])
    reaction_state_store = _PermissiveReactionStateFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=event_store,
        binding_resolver=binding_resolver,
        reaction_router=reaction_router,
        reaction_config={"enabled": True, "merged": False},
        reaction_state_store=reaction_state_store,
        journal=journal,
        publish_processor=processor,
    )

    first = service.ingest_event("github:event-1", thread_target_id="thread-explicit")
    second = service.ingest_event(event)

    assert event_store.lookups == ["github:event-1"]
    assert binding_resolver.calls == [
        ("github:event-1", "thread-explicit"),
        ("github:event-1", None),
    ]
    assert reaction_router.calls == [
        ("github:event-1", "binding-1", ScmReactionConfig(merged=False)),
        ("github:event-1", "binding-1", ScmReactionConfig(merged=False)),
    ]
    assert [intent.operation_key for intent in first.reaction_intents] == [
        "scm:key-1",
        "scm:key-1",
        "scm:key-2",
    ]
    assert [operation.operation_id for operation in first.publish_operations] == [
        "op-1",
        "op-2",
    ]
    assert [operation.operation_id for operation in second.publish_operations] == [
        "op-1",
        "op-2",
    ]
    assert journal.create_calls == [
        ("scm:key-1", "enqueue_managed_turn"),
        ("scm:key-2", "notify_chat"),
        ("scm:key-1", "enqueue_managed_turn"),
        ("scm:key-2", "notify_chat"),
    ]
    assert len(reaction_state_store.should_calls) == 6
    assert len(reaction_state_store.mark_calls) == 4
    assert sorted(journal.operations_by_key) == ["scm:key-1", "scm:key-2"]


def test_ingest_event_suppresses_repeated_semantic_reaction_conditions_using_durable_state(
    tmp_path: Path,
) -> None:
    first_event = _event(event_id="github:event-1")
    second_event = _event(event_id="github:event-2")
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first_event, second_event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=ScmReactionStateStore(tmp_path),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first = service.ingest_event("github:event-1")
    second = service.ingest_event("github:event-2")

    assert len(first.reaction_intents) == 1
    assert len(first.publish_operations) == 1
    first_operation_key = first.publish_operations[0].operation_key
    assert first_operation_key.startswith("scm-reaction:github:changes_requested:")
    assert len(second.reaction_intents) == 1
    assert second.publish_operations == ()
    assert journal.create_calls == [(first_operation_key, "enqueue_managed_turn")]

    state_store = ScmReactionStateStore(tmp_path)
    fingerprint = state_store.compute_reaction_fingerprint(
        first_event,
        binding=binding,
        intent=first.reaction_intents[0],
    )
    stored = state_store.get_reaction_state(
        binding_id=binding.binding_id,
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
    )

    assert stored is not None
    assert stored.state == "emitted"
    assert stored.attempt_count == 2
    assert stored.last_event_id == "github:event-2"


def test_process_now_delegates_to_publish_processor(tmp_path: Path) -> None:
    processed = [
        _operation(
            operation_id="op-processed",
            operation_key="scm:key-1",
            operation_kind="notify_chat",
            state="succeeded",
        )
    ]
    processor = _ProcessorFake(processed=processed)
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=_ReactionRouterFake([]),
        journal=_JournalFake(),
        publish_processor=processor,
    )

    result = service.process_now(limit=7)

    assert processor.calls == [7]
    assert result == processed


def test_ingest_event_escalates_after_duplicate_threshold_without_requeueing_same_reaction(
    tmp_path: Path,
) -> None:
    first_event = _event(
        event_id="github:event-1",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add webhook coverage.",
        },
    )
    second_event = _event(
        event_id="github:event-2",
        payload=dict(first_event.payload),
    )
    third_event = _event(
        event_id="github:event-3",
        payload=dict(first_event.payload),
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first_event, second_event, third_event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=ScmReactionStateStore(tmp_path),
        reaction_config={"duplicate_escalation_threshold": 3},
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first = service.ingest_event("github:event-1")
    second = service.ingest_event("github:event-2")
    third = service.ingest_event("github:event-3")

    assert len(first.publish_operations) == 1
    assert second.publish_operations == ()
    assert len(third.publish_operations) == 1
    assert first.publish_operations[0].operation_kind == "enqueue_managed_turn"
    assert third.publish_operations[0].operation_kind == "notify_chat"
    assert "escalation" in third.publish_operations[0].payload["message"].lower()
    assert journal.create_calls == [
        (first.publish_operations[0].operation_key, "enqueue_managed_turn"),
        (third.publish_operations[0].operation_key, "notify_chat"),
    ]

    state_store = ScmReactionStateStore(tmp_path)
    fingerprint = state_store.compute_reaction_fingerprint(
        first_event,
        binding=binding,
        intent=first.reaction_intents[0],
    )
    stored = state_store.get_reaction_state(
        binding_id=binding.binding_id,
        reaction_kind="changes_requested",
        fingerprint=fingerprint,
    )

    assert stored is not None
    assert stored.state == "emitted"
    assert stored.attempt_count == 3
    assert stored.escalated_at is not None
    assert stored.last_operation_key == third.publish_operations[0].operation_key


def test_ingest_event_resolves_previous_fingerprint_and_allows_reemit_after_condition_changes(
    tmp_path: Path,
) -> None:
    first_event = _event(
        event_id="github:event-1",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add webhook coverage.",
        },
    )
    changed_event = _event(
        event_id="github:event-2",
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add migration coverage.",
        },
    )
    recurring_event = _event(
        event_id="github:event-3",
        payload=dict(first_event.payload),
    )
    binding = _binding()
    journal = _JournalFake()
    state_store = ScmReactionStateStore(tmp_path)
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first_event, changed_event, recurring_event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=state_store,
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first = service.ingest_event("github:event-1")
    second = service.ingest_event("github:event-2")
    third = service.ingest_event("github:event-3")

    assert len(first.publish_operations) == 1
    assert len(second.publish_operations) == 1
    assert len(third.publish_operations) == 1

    first_fingerprint = state_store.compute_reaction_fingerprint(
        first_event,
        binding=binding,
        intent=first.reaction_intents[0],
    )
    changed_fingerprint = state_store.compute_reaction_fingerprint(
        changed_event,
        binding=binding,
        intent=second.reaction_intents[0],
    )
    first_state = state_store.get_reaction_state(
        binding_id=binding.binding_id,
        reaction_kind="changes_requested",
        fingerprint=first_fingerprint,
    )
    changed_state = state_store.get_reaction_state(
        binding_id=binding.binding_id,
        reaction_kind="changes_requested",
        fingerprint=changed_fingerprint,
    )

    assert first_state is not None
    assert first_state.state == "emitted"
    assert first_state.resolved_at is None
    assert changed_state is not None
    assert changed_state.state == "resolved"
    assert changed_state.resolved_at is not None


def test_process_now_escalates_after_repeated_publish_failures_and_marks_recovery(
    tmp_path: Path,
) -> None:
    event = _event(
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Please add webhook coverage.",
        }
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=ScmReactionStateStore(tmp_path),
        reaction_config={"delivery_failure_escalation_threshold": 2},
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    ingested = service.ingest_event(event)
    original = ingested.publish_operations[0]

    first_failed = _operation(
        operation_id=original.operation_id,
        operation_key=original.operation_key,
        operation_kind=original.operation_kind,
        state="pending",
    )
    first_failed = PublishOperation(
        **{
            **first_failed.to_dict(),
            "payload": dict(original.payload),
            "last_error_text": "RuntimeError: temporary outage",
            "attempt_count": 1,
        }
    )
    second_failed = PublishOperation(
        **{
            **first_failed.to_dict(),
            "state": "failed",
            "attempt_count": 2,
        }
    )
    recovered = PublishOperation(
        **{
            **first_failed.to_dict(),
            "state": "succeeded",
            "last_error_text": None,
            "attempt_count": 3,
        }
    )
    processor = _ProcessorSequenceFake(
        [first_failed],
        [second_failed],
        [],
        [recovered],
    )
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=ScmReactionStateStore(tmp_path),
        reaction_config={"delivery_failure_escalation_threshold": 2},
        journal=journal,
        publish_processor=processor,
    )

    first_result = service.process_now(limit=10)
    second_result = service.process_now(limit=10)
    third_result = service.process_now(limit=10)

    assert [operation.state for operation in first_result] == ["pending"]
    assert [operation.state for operation in second_result] == ["failed"]
    assert processor.calls == [10, 10, 1, 10]
    escalation_ops = [
        operation
        for operation in journal.operations_by_key.values()
        if operation.operation_kind == "notify_chat"
        and operation.operation_key != original.operation_key
    ]
    assert len(escalation_ops) == 1
    assert "failed delivery 2 times" in escalation_ops[0].payload["message"]
    assert [operation.state for operation in third_result] == ["succeeded"]

    state_store = ScmReactionStateStore(tmp_path)
    tracking = original.payload["scm_reaction"]
    stored = state_store.get_reaction_state(
        binding_id=tracking["binding_id"],
        reaction_kind=tracking["reaction_kind"],
        fingerprint=tracking["fingerprint"],
    )

    assert stored is not None
    assert stored.state == "emitted"
    assert stored.delivery_failure_count == 2
    assert stored.escalated_at is not None
    assert stored.last_error_text is None
