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


def _ci_failed_event(
    *,
    event_id: str,
    head_sha: str = "deadbeef",
) -> ScmEvent:
    return ScmEvent(
        event_id=event_id,
        provider="github",
        event_type="check_run",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "completed",
            "status": "completed",
            "conclusion": "failure",
            "name": "ci / test",
            "head_sha": head_sha,
        },
        raw_payload=None,
    )


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
        self.next_attempts_by_key: dict[str, Optional[str]] = {}
        self.update_calls: list[str] = []

    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]:
        self.create_calls.append((operation_key, operation_kind))
        self.next_attempts_by_key[operation_key] = next_attempt_at
        existing = self.operations_by_key.get(operation_key)
        if existing is not None:
            return existing, True
        created = _operation(
            operation_id=f"op-{len(self.operations_by_key) + 1}",
            operation_key=operation_key,
            operation_kind=operation_kind,
        )
        created = PublishOperation(
            **{
                **created.to_dict(),
                "payload": dict(payload or {}),
                "next_attempt_at": next_attempt_at or created.next_attempt_at,
            }
        )
        self.operations_by_key[operation_key] = created
        return created, False

    def update_pending_operation(
        self,
        operation_id: str,
        *,
        payload: Optional[dict] = None,
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        self.update_calls.append(operation_id)
        for key, operation in list(self.operations_by_key.items()):
            if operation.operation_id != operation_id or operation.state != "pending":
                continue
            updated = PublishOperation(
                **{
                    **operation.to_dict(),
                    "payload": dict(payload or operation.payload),
                    "next_attempt_at": next_attempt_at or operation.next_attempt_at,
                }
            )
            self.operations_by_key[key] = updated
            self.next_attempts_by_key[key] = updated.next_attempt_at
            return updated
        return None

    def list_operations(
        self,
        *,
        state: Optional[str] = None,
        operation_kind: Optional[str] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> list[PublishOperation]:
        operations = list(self.operations_by_key.values())
        if state is not None:
            operations = [op for op in operations if op.state == state]
        if operation_kind is not None:
            operations = [
                op for op in operations if op.operation_kind == operation_kind
            ]
        operations.sort(
            key=lambda op: (op.created_at, op.operation_id), reverse=newest_first
        )
        if limit is not None:
            operations = operations[:limit]
        return operations


class _JournalUpdateRaceFake(_JournalFake):
    """Simulates losing a race when merging into a pending publish operation."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next_update_for: set[str] = set()

    def arm_fail_next_update(self, operation_id: str) -> None:
        self._fail_next_update_for.add(operation_id)

    def update_pending_operation(
        self,
        operation_id: str,
        *,
        payload: Optional[dict] = None,
        next_attempt_at: Optional[str] = None,
    ) -> Optional[PublishOperation]:
        self.update_calls.append(operation_id)
        if operation_id in self._fail_next_update_for:
            self._fail_next_update_for.discard(operation_id)
            return None
        return super().update_pending_operation(
            operation_id,
            payload=payload,
            next_attempt_at=next_attempt_at,
        )


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


class _FailingReactionStateFake(_PermissiveReactionStateFake):
    def __init__(self) -> None:
        super().__init__()
        self.failed_calls = 0
        self.escalated_calls = 0

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
        self.failed_calls += 1
        if self.failed_calls == 1:
            raise RuntimeError("reaction state unavailable")
        return SimpleNamespace(escalated_at=None, delivery_failure_count=1)

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
        self.escalated_calls += 1
        raise RuntimeError("escalation state unavailable")


class _EscalationStateFailureFake(_PermissiveReactionStateFake):
    def __init__(self) -> None:
        super().__init__()
        self.escalated_calls = 0

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
        self.escalated_calls += 1
        raise RuntimeError("escalation state unavailable")


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


def test_ci_failed_pending_merge_falls_back_to_create_when_update_races(
    tmp_path: Path,
) -> None:
    head_sha = "abc123deadbeef0000"
    event_a = _ci_failed_event(event_id="github:event-a", head_sha=head_sha)
    event_b = _ci_failed_event(event_id="github:event-b", head_sha=head_sha)
    binding = _binding()
    journal = _JournalUpdateRaceFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event_a, event_b),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first = service.ingest_event("github:event-a")
    assert len(first.publish_operations) == 1
    pending = first.publish_operations[0]
    journal.arm_fail_next_update(pending.operation_id)

    second = service.ingest_event("github:event-b")
    assert len(second.publish_operations) == 1
    merged_op = second.publish_operations[0]
    assert merged_op.operation_id != pending.operation_id
    request = merged_op.payload.get("request")
    assert isinstance(request, dict)
    metadata = request.get("metadata")
    assert isinstance(metadata, dict)
    bundle = metadata.get("scm_feedback_bundle")
    assert isinstance(bundle, dict)
    event_ids = {
        item["event_id"]
        for item in bundle.get("items", [])
        if isinstance(item, dict) and "event_id" in item
    }
    assert event_ids == {"github:event-a", "github:event-b"}
    assert journal.create_calls[-1][1] == "enqueue_managed_turn"


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


def test_handle_processed_operations_continues_when_reaction_state_update_fails(
    tmp_path: Path,
) -> None:
    reaction_state_store = _FailingReactionStateFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=_ReactionRouterFake([]),
        reaction_state_store=reaction_state_store,
        journal=_JournalFake(),
        publish_processor=_ProcessorFake(processed=[]),
    )

    first = PublishOperation(
        **{
            **_operation(
                operation_id="op-1",
                operation_key="scm:key-1",
                operation_kind="notify_chat",
                state="failed",
            ).to_dict(),
            "payload": {
                "scm_reaction": {
                    "binding_id": "binding-1",
                    "reaction_kind": "changes_requested",
                    "fingerprint": "fp-1",
                    "event_id": "github:event-1",
                }
            },
            "last_error_text": "RuntimeError: first failure",
        }
    )
    second = PublishOperation(
        **{
            **_operation(
                operation_id="op-2",
                operation_key="scm:key-2",
                operation_kind="notify_chat",
                state="failed",
            ).to_dict(),
            "payload": {
                "scm_reaction": {
                    "binding_id": "binding-1",
                    "reaction_kind": "changes_requested",
                    "fingerprint": "fp-2",
                    "event_id": "github:event-2",
                }
            },
            "last_error_text": "RuntimeError: second failure",
        }
    )

    escalations = service._handle_processed_operations([first, second])

    assert escalations == []
    assert reaction_state_store.failed_calls == 2


def test_handle_processed_operations_keeps_escalation_operation_when_escalation_state_write_fails(
    tmp_path: Path,
) -> None:
    reaction_state_store = _EscalationStateFailureFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=_ReactionRouterFake([]),
        reaction_config={"delivery_failure_escalation_threshold": 1},
        reaction_state_store=reaction_state_store,
        journal=_JournalFake(),
        publish_processor=_ProcessorFake(processed=[]),
    )

    failed = PublishOperation(
        **{
            **_operation(
                operation_id="op-3",
                operation_key="scm:key-3",
                operation_kind="notify_chat",
                state="failed",
            ).to_dict(),
            "payload": {
                "thread_target_id": "thread-123",
                "scm_reaction": {
                    "binding_id": "binding-1",
                    "reaction_kind": "changes_requested",
                    "fingerprint": "fp-3",
                    "event_id": "github:event-3",
                    "thread_target_id": "thread-123",
                    "repo_id": "repo-1",
                },
            },
            "last_error_text": "RuntimeError: delivery failed",
        }
    )

    escalations = service._handle_processed_operations([failed])

    assert len(escalations) == 1
    assert escalations[0].operation_kind == "notify_chat"
    assert reaction_state_store.escalated_calls == 1


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


def test_ingest_event_tracks_review_comment_operations_in_separate_state_namespaces(
    tmp_path: Path,
) -> None:
    event = ScmEvent(
        event_id="github:event-inline-comment",
        provider="github",
        event_type="pull_request_review_comment",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "created",
            "comment_id": "2844",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Please cover the inline review-comment webhook path too.",
        },
        raw_payload=None,
    )
    binding = _binding()
    state_store = _PermissiveReactionStateFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=state_store,
        journal=_JournalFake(),
        publish_processor=_ProcessorFake(processed=[]),
    )

    result = service.ingest_event(event.event_id)

    assert [operation.operation_kind for operation in result.publish_operations] == [
        "react_pr_review_comment",
        "enqueue_managed_turn",
        "notify_chat",
    ]
    assert state_store.should_calls == [
        (
            "binding-1",
            "review_comment:react_pr_review_comment",
            "review_comment:github:event-inline-comment:react_pr_review_comment",
        ),
        (
            "binding-1",
            "review_comment:enqueue_managed_turn",
            "review_comment:github:event-inline-comment:enqueue_managed_turn",
        ),
    ]
    tracking_payloads = [
        operation.payload["scm_reaction"]
        for operation in result.publish_operations
        if "scm_reaction" in operation.payload
    ]
    assert [payload["reaction_kind"] for payload in tracking_payloads] == [
        "review_comment",
        "review_comment",
    ]
    assert [payload["reaction_state_kind"] for payload in tracking_payloads] == [
        "review_comment:react_pr_review_comment",
        "review_comment:enqueue_managed_turn",
    ]


def test_ingest_event_batches_review_comment_enqueue_for_15_seconds(
    tmp_path: Path,
) -> None:
    event = ScmEvent(
        event_id="github:event-inline-comment-formatted",
        provider="github",
        event_type="pull_request_review_comment",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:14Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "created",
            "comment_id": "2844",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "**<sub><sub>!P1 Badge</sub></sub>** Surface startup timeouts as fallback regressions.",
            "path": "src/codex_autorunner/integrations/discord/message_turns.py",
            "line": 942,
            "html_url": "https://github.com/acme/widgets/pull/42#discussion_r2844",
        },
        raw_payload=None,
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    result = service.ingest_event(event.event_id)

    enqueue_op = next(
        operation
        for operation in result.publish_operations
        if operation.operation_kind == "enqueue_managed_turn"
    )
    notify_op = next(
        operation
        for operation in result.publish_operations
        if operation.operation_kind == "notify_chat"
    )
    assert enqueue_op.next_attempt_at == "2026-03-26T00:00:15Z"
    assert notify_op.next_attempt_at == "2026-03-26T00:00:15Z"
    assert journal.next_attempts_by_key[enqueue_op.operation_key] == (
        "2026-03-26T00:00:15Z"
    )


def test_ingest_event_dedupes_review_comment_enqueue_within_same_batch_window(
    tmp_path: Path,
) -> None:
    first = ScmEvent(
        event_id="github:event-inline-comment-generics",
        provider="github",
        event_type="pull_request_review_comment",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "created",
            "comment_id": "2845",
            "author_login": "reviewer",
            "author_type": "User",
            "issue_author_login": "pr-author",
            "body": "Keep Response<T> and <foo> examples intact while removing <sub>badge</sub> wrappers.",
            "path": "src/codex_autorunner/core/scm_automation_service.py",
            "line": 300,
        },
        raw_payload=None,
    )
    second = ScmEvent(
        **{
            **first.to_dict(),
            "event_id": "github:event-inline-comment-generics-2",
            "received_at": "2026-03-26T00:00:13Z",
            "payload": {
                **first.payload,
                "comment_id": "2846",
                "body": "Also keep batching stable within the first 15 seconds.",
            },
        }
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first, second),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first_result = service.ingest_event(first.event_id)
    second_result = service.ingest_event(second.event_id)

    first_enqueue = next(
        operation
        for operation in first_result.publish_operations
        if operation.operation_kind == "enqueue_managed_turn"
    )
    second_enqueue = next(
        operation
        for operation in second_result.publish_operations
        if operation.operation_kind == "enqueue_managed_turn"
    )
    assert first_enqueue.operation_key == second_enqueue.operation_key
    assert (
        journal.create_calls.count(
            (first_enqueue.operation_key, "enqueue_managed_turn")
        )
        == 2
    )


def test_ingest_event_batches_ci_failures_for_same_head_and_refreshes_timer(
    tmp_path: Path,
) -> None:
    first = ScmEvent(
        event_id="github:check-1",
        provider="github",
        event_type="check_run",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "completed",
            "status": "completed",
            "conclusion": "failure",
            "name": "chat-apps",
            "head_sha": "abc123",
        },
        raw_payload=None,
    )
    second = ScmEvent(
        **{
            **first.to_dict(),
            "event_id": "github:check-2",
            "received_at": "2026-03-26T00:00:40Z",
            "payload": {
                **first.payload,
                "name": "check",
            },
        }
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first, second),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first_result = service.ingest_event(first.event_id)
    second_result = service.ingest_event(second.event_id)

    assert len(first_result.publish_operations) == 1
    assert len(second_result.publish_operations) == 1
    assert first_result.publish_operations[0].operation_id == "op-1"
    assert second_result.publish_operations[0].operation_id == "op-1"
    assert journal.create_calls == [
        (first_result.publish_operations[0].operation_key, "enqueue_managed_turn")
    ]
    assert journal.update_calls == ["op-1"]
    assert first_result.publish_operations[0].next_attempt_at == "2026-03-26T00:01:01Z"
    assert second_result.publish_operations[0].next_attempt_at == "2026-03-26T00:01:40Z"
    assert (
        second_result.publish_operations[0].payload["request"]["message_text"]
        == "CI failed for acme/widgets#42: chat-apps (failure). Inspect the failing check and push a fix."
    )


def test_ingest_event_suppresses_late_ci_failure_for_already_handled_head(
    tmp_path: Path,
) -> None:
    first = _ci_failed_event(event_id="github:check-1", head_sha="abc123")
    second = ScmEvent(
        **{
            **first.to_dict(),
            "event_id": "github:check-2",
            "payload": {
                **first.payload,
                "name": "aggregate",
            },
        }
    )
    binding = _binding()
    journal = _JournalFake()
    state_store = ScmReactionStateStore(tmp_path)
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first, second),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=state_store,
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    first_result = service.ingest_event(first.event_id)
    first_operation = first_result.publish_operations[0]
    journal.operations_by_key[first_operation.operation_key] = PublishOperation(
        **{
            **first_operation.to_dict(),
            "state": "succeeded",
        }
    )

    second_result = service.ingest_event(second.event_id)

    assert second_result.publish_operations == ()
    assert journal.create_calls == [
        (first_operation.operation_key, "enqueue_managed_turn")
    ]
    second_fingerprint = state_store.compute_reaction_fingerprint(
        second,
        binding=binding,
        intent=second_result.reaction_intents[0],
    )
    suppressed = state_store.get_reaction_state(
        binding_id=binding.binding_id,
        reaction_kind="ci_failed",
        fingerprint=second_fingerprint,
    )
    assert suppressed is not None
    assert suppressed.state == "suppressed"
    assert suppressed.metadata["ci_head_sha"] == "abc123"
    assert suppressed.metadata["suppression_reason"] == "ci_head_already_queued"


def test_ingest_event_ci_failure_batch_refresh_honors_max_window(
    tmp_path: Path,
) -> None:
    first = ScmEvent(
        event_id="github:ci-cap-1",
        provider="github",
        event_type="check_run",
        occurred_at="2026-03-26T00:00:00Z",
        received_at="2026-03-26T00:00:01Z",
        created_at="2026-03-26T00:00:02Z",
        repo_slug="acme/widgets",
        repo_id="repo-1",
        pr_number=42,
        delivery_id="delivery-1",
        payload={
            "action": "completed",
            "status": "completed",
            "conclusion": "failure",
            "name": "chat-apps",
            "head_sha": "abc123",
        },
        raw_payload=None,
    )
    second = ScmEvent(
        **{
            **first.to_dict(),
            "event_id": "github:ci-cap-2",
            "received_at": "2026-03-26T00:02:50Z",
            "payload": {
                **first.payload,
                "name": "check",
            },
        }
    )
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(first, second),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    service.ingest_event(first.event_id)
    second_result = service.ingest_event(second.event_id)

    assert second_result.publish_operations[0].next_attempt_at == "2026-03-26T00:03:01Z"


def test_ingest_event_creates_review_comment_notice_dependency(
    tmp_path: Path,
) -> None:
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(
            ScmEvent(
                event_id="github:event-inline-comment",
                provider="github",
                event_type="pull_request_review_comment",
                occurred_at="2026-03-26T00:00:00Z",
                received_at="2026-03-26T00:00:01Z",
                created_at="2026-03-26T00:00:02Z",
                repo_slug="acme/widgets",
                repo_id="repo-1",
                pr_number=42,
                delivery_id="delivery-1",
                payload={
                    "action": "created",
                    "comment_id": "2844",
                    "author_login": "reviewer",
                    "author_type": "User",
                    "issue_author_login": "pr-author",
                    "body": "Please cover the inline review-comment webhook path too.",
                },
                raw_payload=None,
            )
        ),
        binding_resolver=_BindingResolverFake(_binding()),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    result = service.ingest_event("github:event-inline-comment")

    enqueue_op = next(
        operation
        for operation in result.publish_operations
        if operation.operation_kind == "enqueue_managed_turn"
    )
    notify_op = next(
        operation
        for operation in result.publish_operations
        if operation.operation_kind == "notify_chat"
    )

    assert notify_op.payload["thread_target_id"] == "thread-123"
    dependency = notify_op.payload["managed_turn_dependency"]
    assert dependency["dependency_kind"] == "enqueue_managed_turn_started"
    assert dependency["operation_id"] == enqueue_op.operation_id
    assert (
        "Started the latest PR review batch for acme/widgets#42."
        in notify_op.payload["message"]
    )


def test_handle_processed_operations_skips_pending_notify_start_retries(
    tmp_path: Path,
) -> None:
    class _TrackingReactionStateFake(_PermissiveReactionStateFake):
        def __init__(self) -> None:
            super().__init__()
            self.failed_calls: list[tuple[str, str, str, Optional[str]]] = []

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
            _ = metadata
            self.failed_calls.append(
                (binding_id, reaction_kind, fingerprint, error_text)
            )
            return SimpleNamespace(escalated_at=None, delivery_failure_count=1)

    reaction_state = _TrackingReactionStateFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=_ReactionRouterFake([]),
        reaction_state_store=reaction_state,
        journal=_JournalFake(),
        publish_processor=_ProcessorFake(processed=[]),
    )
    pending_notify = PublishOperation(
        **{
            **_operation(
                operation_id="op-notify-pending",
                operation_key="scm:notify-pending",
                operation_kind="notify_chat",
                state="pending",
            ).to_dict(),
            "payload": {
                "scm_reaction": {
                    "binding_id": "binding-1",
                    "reaction_kind": "review_comment",
                    "reaction_state_kind": "review_comment:notify_chat",
                    "fingerprint": "fp-inline",
                    "event_id": "github:event-inline-comment",
                    "operation_kind": "notify_chat",
                    "repo_slug": "acme/widgets",
                    "pr_number": 42,
                    "thread_target_id": "thread-123",
                },
                "managed_turn_dependency": {
                    "dependency_kind": "enqueue_managed_turn_started",
                    "operation_id": "op-enqueue-1",
                    "thread_target_id": "thread-123",
                },
            },
            "last_error_text": (
                "RetryablePublishError: Waiting for managed turn start confirmation"
            ),
        }
    )

    follow_ups = service._handle_processed_operations([pending_notify])

    assert follow_ups == []
    assert reaction_state.failed_calls == []


def test_handle_processed_operations_uses_reaction_state_kind_tracking_key(
    tmp_path: Path,
) -> None:
    state_store = ScmReactionStateStore(tmp_path)
    state_store.mark_reaction_emitted(
        binding_id="binding-1",
        reaction_kind="review_comment:react_pr_review_comment",
        fingerprint="fp-inline",
        event_id="github:event-inline-comment",
        operation_key="scm:key-inline",
    )
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=_ReactionRouterFake([]),
        reaction_state_store=state_store,
        journal=_JournalFake(),
        publish_processor=_ProcessorFake(processed=[]),
    )
    failed = PublishOperation(
        **{
            **_operation(
                operation_id="op-inline",
                operation_key="scm:key-inline",
                operation_kind="react_pr_review_comment",
                state="failed",
            ).to_dict(),
            "payload": {
                "scm_reaction": {
                    "binding_id": "binding-1",
                    "reaction_kind": "review_comment",
                    "reaction_state_kind": "review_comment:react_pr_review_comment",
                    "fingerprint": "fp-inline",
                    "event_id": "github:event-inline-comment",
                    "operation_kind": "react_pr_review_comment",
                }
            },
            "last_error_text": "RuntimeError: delivery failed",
        }
    )

    escalations = service._handle_processed_operations([failed])

    assert escalations == []
    stored = state_store.get_reaction_state(
        binding_id="binding-1",
        reaction_kind="review_comment:react_pr_review_comment",
        fingerprint="fp-inline",
    )
    assert stored is not None
    assert stored.state == "delivery_failed"
    assert stored.delivery_failure_count == 1


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


def test_ingest_event_duplicate_escalation_dedupes_journal_operations(
    tmp_path: Path,
) -> None:
    events = [_event(event_id=f"github:escal-dedup-{i}") for i in range(5)]
    binding = _binding()
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(*events),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=ScmReactionStateStore(tmp_path),
        reaction_config={"duplicate_escalation_threshold": 3},
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    for ev in events:
        service.ingest_event(ev.event_id)

    escalation_ops = [
        op
        for op in journal.operations_by_key.values()
        if op.operation_kind == "notify_chat"
    ]
    escalation_keys = {op.operation_key for op in escalation_ops}
    assert len(escalation_keys) == 1


def test_process_now_delivery_failure_escalation_threshold_resets_on_recovery(
    tmp_path: Path,
) -> None:
    event = _event(
        payload={
            "action": "submitted",
            "review_state": "changes_requested",
            "author_login": "reviewer",
            "body": "Test delivery failure recovery.",
        }
    )
    binding = _binding()
    journal = _JournalFake()
    state_store = ScmReactionStateStore(tmp_path)
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(binding),
        reaction_router=route_scm_reactions,
        reaction_state_store=state_store,
        reaction_config={"delivery_failure_escalation_threshold": 1},
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )
    ingested = service.ingest_event(event)
    original_op = ingested.publish_operations[0]

    failed_op = PublishOperation(
        **{
            **original_op.to_dict(),
            "state": "failed",
            "attempt_count": 1,
            "last_error_text": "timeout",
        }
    )
    service._handle_processed_operations([failed_op])

    tracking = original_op.payload["scm_reaction"]
    state = state_store.get_reaction_state(
        binding_id=tracking["binding_id"],
        reaction_kind=tracking["reaction_kind"],
        fingerprint=tracking["fingerprint"],
    )
    assert state is not None
    assert state.delivery_failure_count == 1
    assert state.escalated_at is not None

    escalation_ops = [
        op
        for op in journal.operations_by_key.values()
        if op.operation_kind == "notify_chat"
        and op.operation_key != original_op.operation_key
    ]
    assert len(escalation_ops) == 1


def test_ingest_event_no_binding_notify_chat_has_no_binding_id(
    tmp_path: Path,
) -> None:
    event = _event(event_id="github:no-bind-notify")
    journal = _JournalFake()
    service = ScmAutomationService(
        tmp_path,
        event_store=_EventStoreFake(event),
        binding_resolver=_BindingResolverFake(None),
        reaction_router=route_scm_reactions,
        reaction_state_store=_PermissiveReactionStateFake(),
        journal=journal,
        publish_processor=_ProcessorFake(processed=[]),
    )

    result = service.ingest_event(event)

    assert result.binding is None
    assert len(result.publish_operations) == 1
    notify_op = result.publish_operations[0]
    assert notify_op.operation_kind == "notify_chat"
    assert notify_op.payload.get("binding_id") is None
