from __future__ import annotations

from pathlib import Path
from typing import Optional

from codex_autorunner.core.pr_bindings import PrBinding
from codex_autorunner.core.publish_journal import PublishOperation
from codex_autorunner.core.scm_automation_service import ScmAutomationService
from codex_autorunner.core.scm_events import ScmEvent
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
    service = ScmAutomationService(
        tmp_path,
        event_store=event_store,
        binding_resolver=binding_resolver,
        reaction_router=reaction_router,
        reaction_config={"enabled": True, "merged": False},
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
    assert sorted(journal.operations_by_key) == ["scm:key-1", "scm:key-2"]


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
