from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .pr_binding_resolver import resolve_binding_for_scm_event
from .pr_bindings import PrBinding
from .publish_executor import PublishOperationProcessor
from .publish_journal import PublishJournalStore, PublishOperation
from .publish_operation_executors import (
    build_enqueue_managed_turn_executor,
    build_notify_chat_executor,
)
from .scm_events import ScmEvent, ScmEventStore
from .scm_reaction_router import route_scm_reactions
from .scm_reaction_types import ReactionIntent, ScmReactionConfig


class ScmEventLookup(Protocol):
    def get_event(self, event_id: str) -> Optional[ScmEvent]: ...


class ScmBindingResolver(Protocol):
    def __call__(
        self,
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]: ...


class ScmReactionRouter(Protocol):
    def __call__(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding] = None,
        config: ScmReactionConfig | Mapping[str, Any] | None = None,
    ) -> list[ReactionIntent]: ...


class PublishJournalWriter(Protocol):
    def create_operation(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
    ) -> tuple[PublishOperation, bool]: ...


class PublishOperationDrainer(Protocol):
    def process_now(self, limit: int = 10) -> list[PublishOperation]: ...


def _normalize_event_id(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _default_binding_resolver(hub_root: Path) -> ScmBindingResolver:
    def resolver(
        event: ScmEvent,
        *,
        thread_target_id: Optional[str] = None,
    ) -> Optional[PrBinding]:
        return resolve_binding_for_scm_event(
            hub_root,
            event,
            thread_target_id=thread_target_id,
        )

    return resolver


def _default_publish_processor(
    hub_root: Path,
    *,
    journal: PublishJournalStore,
) -> PublishOperationProcessor:
    return PublishOperationProcessor(
        journal,
        executors={
            "enqueue_managed_turn": build_enqueue_managed_turn_executor(
                hub_root=hub_root
            ),
            "notify_chat": build_notify_chat_executor(hub_root=hub_root),
        },
    )


@dataclass(frozen=True)
class ScmAutomationIngestResult:
    event: ScmEvent
    binding: Optional[PrBinding]
    reaction_intents: tuple[ReactionIntent, ...]
    publish_operations: tuple[PublishOperation, ...]


class ScmAutomationService:
    def __init__(
        self,
        hub_root: Path,
        *,
        event_store: Optional[ScmEventLookup] = None,
        binding_resolver: Optional[ScmBindingResolver] = None,
        reaction_router: Optional[ScmReactionRouter] = None,
        reaction_config: ScmReactionConfig | Mapping[str, Any] | None = None,
        journal: Optional[PublishJournalWriter] = None,
        publish_processor: Optional[PublishOperationDrainer] = None,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._event_store = event_store or ScmEventStore(self._hub_root)
        self._binding_resolver = binding_resolver or _default_binding_resolver(
            self._hub_root
        )
        self._reaction_router = reaction_router or route_scm_reactions
        self._reaction_config = ScmReactionConfig.from_mapping(reaction_config)
        resolved_journal = journal or PublishJournalStore(self._hub_root)
        self._journal = resolved_journal
        if publish_processor is not None:
            self._publish_processor = publish_processor
        elif isinstance(resolved_journal, PublishJournalStore):
            self._publish_processor = _default_publish_processor(
                self._hub_root,
                journal=resolved_journal,
            )
        else:
            raise TypeError(
                "publish_processor is required when journal is not a PublishJournalStore"
            )

    def _resolve_event(self, event_or_id: ScmEvent | str) -> ScmEvent:
        if isinstance(event_or_id, ScmEvent):
            return event_or_id
        event_id = _normalize_event_id(event_or_id)
        if event_id is None:
            raise ValueError("event_or_id must be a ScmEvent or non-empty event_id")
        event = self._event_store.get_event(event_id)
        if event is None:
            raise LookupError(f"SCM event '{event_id}' was not found")
        return event

    def ingest_event(
        self,
        event_or_id: ScmEvent | str,
        *,
        thread_target_id: Optional[str] = None,
    ) -> ScmAutomationIngestResult:
        event = self._resolve_event(event_or_id)
        binding = self._binding_resolver(event, thread_target_id=thread_target_id)
        reaction_intents = tuple(
            self._reaction_router(
                event,
                binding=binding,
                config=self._reaction_config,
            )
        )

        publish_operations: list[PublishOperation] = []
        seen_operation_keys: set[str] = set()
        for intent in reaction_intents:
            if intent.operation_key in seen_operation_keys:
                continue
            seen_operation_keys.add(intent.operation_key)
            operation, _deduped = self._journal.create_operation(
                operation_key=intent.operation_key,
                operation_kind=intent.operation_kind,
                payload=intent.payload,
            )
            publish_operations.append(operation)

        return ScmAutomationIngestResult(
            event=event,
            binding=binding,
            reaction_intents=reaction_intents,
            publish_operations=tuple(publish_operations),
        )

    def process_now(self, limit: int = 10) -> list[PublishOperation]:
        return self._publish_processor.process_now(limit=limit)


__all__ = [
    "PublishJournalWriter",
    "PublishOperationDrainer",
    "ScmAutomationIngestResult",
    "ScmAutomationService",
    "ScmBindingResolver",
    "ScmEventLookup",
    "ScmReactionRouter",
]
