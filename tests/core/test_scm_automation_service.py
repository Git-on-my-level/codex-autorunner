from __future__ import annotations

from pathlib import Path
from typing import Optional

from codex_autorunner.core.pr_bindings import PrBinding
from codex_autorunner.core.publish_journal import PublishOperation
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEvent
from codex_autorunner.core.scm_reaction_router import route_scm_reactions
from codex_autorunner.core.scm_reaction_state import ScmReactionStateStore
from codex_autorunner.core.scm_reaction_types import ReactionIntent, ScmReactionConfig


def _event(*, event_id: str = "github:event-1") -> ScmEvent:
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
        payload={"action": "submitted", "review_state": "changes_requested"},
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
        _ = payload, next_attempt_at
        self.create_calls.append((operation_key, operation_kind))
        existing = self.operations_by_key.get(operation_key)
        if existing is not None:
            return existing, True
        created = _operation(
            operation_id=f"op-{len(self.operations_by_key) + 1}",
            operation_key=operation_key,
            operation_kind=operation_kind,
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
    assert stored.attempt_count == 1
    assert stored.last_event_id == "github:event-1"


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
