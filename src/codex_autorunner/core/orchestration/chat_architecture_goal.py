"""Goal function for the shared chat architecture.

This module makes the desired chat architecture explicit and testable.  It is
not a runtime state machine; it is a pure scoring contract that lets reviews,
tests, and migration tickets compare an implementation against the target
shape:

- orchestration owns lifecycle and durable ordering
- adapters own transport projection only
- surfaces consume read models rather than inventing transcript state
- new agents plug in through runtime descriptors and harness capabilities
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence


class ChatArchitectureDimension(str, Enum):
    """Stable scoring dimensions for the chat architecture goal function."""

    STATE_OWNERSHIP = "state_ownership"
    DURABILITY = "durability"
    SURFACE_PLUGGABILITY = "surface_pluggability"
    AGENT_PLUGGABILITY = "agent_pluggability"
    TESTABILITY = "testability"
    OBSERVABILITY = "observability"


class ChatArchitectureSignal(str, Enum):
    """Positive or negative evidence used by the goal function."""

    CANONICAL_OPERATION_STATE_MACHINE = "canonical_operation_state_machine"
    CANONICAL_THREAD_STATUS_MACHINE = "canonical_thread_status_machine"
    CANONICAL_TURN_LIFECYCLE_CONTRACT = "canonical_turn_lifecycle_contract"
    DURABLE_OPERATION_LEDGER = "durable_operation_ledger"
    DURABLE_THREAD_EXECUTION_STORE = "durable_thread_execution_store"
    DURABLE_DELIVERY_LEDGER = "durable_delivery_ledger"
    PROTOCOL_NEUTRAL_SURFACE_IDENTITY = "protocol_neutral_surface_identity"
    PROTOCOL_NEUTRAL_AGENT_REGISTRY = "protocol_neutral_agent_registry"
    PURE_RECOVERY_AND_PROJECTION_FUNCTIONS = "pure_recovery_and_projection_functions"
    SNAPSHOT_AND_EVENT_READ_MODELS = "snapshot_and_event_read_models"
    SURFACE_LOCAL_LIFECYCLE_MACHINE = "surface_local_lifecycle_machine"
    SURFACE_LOCAL_TRANSCRIPT_ORDERING = "surface_local_transcript_ordering"
    ADAPTER_LOCAL_DELIVERY_RETRY_POLICY = "adapter_local_delivery_retry_policy"
    AGENT_SPECIFIC_ROUTE_BRANCHING = "agent_specific_route_branching"
    OPAQUE_STATUS_STRING_MAPPING = "opaque_status_string_mapping"


POSITIVE_SIGNALS: frozenset[ChatArchitectureSignal] = frozenset(
    {
        ChatArchitectureSignal.CANONICAL_OPERATION_STATE_MACHINE,
        ChatArchitectureSignal.CANONICAL_THREAD_STATUS_MACHINE,
        ChatArchitectureSignal.CANONICAL_TURN_LIFECYCLE_CONTRACT,
        ChatArchitectureSignal.DURABLE_OPERATION_LEDGER,
        ChatArchitectureSignal.DURABLE_THREAD_EXECUTION_STORE,
        ChatArchitectureSignal.DURABLE_DELIVERY_LEDGER,
        ChatArchitectureSignal.PROTOCOL_NEUTRAL_SURFACE_IDENTITY,
        ChatArchitectureSignal.PROTOCOL_NEUTRAL_AGENT_REGISTRY,
        ChatArchitectureSignal.PURE_RECOVERY_AND_PROJECTION_FUNCTIONS,
        ChatArchitectureSignal.SNAPSHOT_AND_EVENT_READ_MODELS,
    }
)

NEGATIVE_SIGNALS: frozenset[ChatArchitectureSignal] = frozenset(
    {
        ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE,
        ChatArchitectureSignal.SURFACE_LOCAL_TRANSCRIPT_ORDERING,
        ChatArchitectureSignal.ADAPTER_LOCAL_DELIVERY_RETRY_POLICY,
        ChatArchitectureSignal.AGENT_SPECIFIC_ROUTE_BRANCHING,
        ChatArchitectureSignal.OPAQUE_STATUS_STRING_MAPPING,
    }
)


@dataclass(frozen=True)
class ChatArchitectureCriterion:
    criterion_id: str
    dimension: ChatArchitectureDimension
    weight: int
    required_signals: frozenset[ChatArchitectureSignal]
    forbidden_signals: frozenset[ChatArchitectureSignal] = frozenset()
    target: str = ""

    def satisfied_by(self, signals: frozenset[ChatArchitectureSignal]) -> bool:
        return self.required_signals.issubset(signals) and not (
            self.forbidden_signals & signals
        )

    def missing_signals(
        self, signals: frozenset[ChatArchitectureSignal]
    ) -> tuple[ChatArchitectureSignal, ...]:
        return tuple(
            sorted(self.required_signals - signals, key=lambda item: item.value)
        )

    def blocking_signals(
        self, signals: frozenset[ChatArchitectureSignal]
    ) -> tuple[ChatArchitectureSignal, ...]:
        return tuple(
            sorted(self.forbidden_signals & signals, key=lambda item: item.value)
        )


@dataclass(frozen=True)
class ChatArchitectureFinding:
    criterion_id: str
    dimension: ChatArchitectureDimension
    weight: int
    satisfied: bool
    target: str
    missing_signals: tuple[ChatArchitectureSignal, ...] = ()
    blocking_signals: tuple[ChatArchitectureSignal, ...] = ()

    @property
    def priority(self) -> int:
        return 0 if self.satisfied else self.weight

    def to_dict(self) -> dict[str, object]:
        return {
            "criterion_id": self.criterion_id,
            "dimension": self.dimension.value,
            "weight": self.weight,
            "satisfied": self.satisfied,
            "target": self.target,
            "missing_signals": [signal.value for signal in self.missing_signals],
            "blocking_signals": [signal.value for signal in self.blocking_signals],
        }


@dataclass(frozen=True)
class ChatArchitectureGoalEvaluation:
    score: int
    max_score: int
    findings: tuple[ChatArchitectureFinding, ...]
    signals: frozenset[ChatArchitectureSignal] = field(default_factory=frozenset)

    @property
    def normalized_score(self) -> float:
        if self.max_score <= 0:
            return 1.0
        return self.score / self.max_score

    @property
    def satisfied(self) -> bool:
        return self.score == self.max_score

    @property
    def gaps(self) -> tuple[ChatArchitectureFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.satisfied)

    def top_gaps(self, *, limit: int = 5) -> tuple[ChatArchitectureFinding, ...]:
        bounded = max(int(limit), 0)
        return tuple(
            sorted(
                self.gaps,
                key=lambda finding: (-finding.priority, finding.criterion_id),
            )[:bounded]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "max_score": self.max_score,
            "normalized_score": self.normalized_score,
            "satisfied": self.satisfied,
            "signals": sorted(signal.value for signal in self.signals),
            "findings": [finding.to_dict() for finding in self.findings],
        }


CHAT_ARCHITECTURE_GOAL_CRITERIA: tuple[ChatArchitectureCriterion, ...] = (
    ChatArchitectureCriterion(
        criterion_id="one_control_plane_state_machine",
        dimension=ChatArchitectureDimension.STATE_OWNERSHIP,
        weight=10,
        required_signals=frozenset(
            {
                ChatArchitectureSignal.CANONICAL_OPERATION_STATE_MACHINE,
                ChatArchitectureSignal.CANONICAL_THREAD_STATUS_MACHINE,
                ChatArchitectureSignal.CANONICAL_TURN_LIFECYCLE_CONTRACT,
            }
        ),
        forbidden_signals=frozenset(
            {
                ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE,
                ChatArchitectureSignal.OPAQUE_STATUS_STRING_MAPPING,
            }
        ),
        target="Lifecycle transitions live in orchestration contracts; adapters project them.",
    ),
    ChatArchitectureCriterion(
        criterion_id="durable_order_and_recovery",
        dimension=ChatArchitectureDimension.DURABILITY,
        weight=9,
        required_signals=frozenset(
            {
                ChatArchitectureSignal.DURABLE_OPERATION_LEDGER,
                ChatArchitectureSignal.DURABLE_THREAD_EXECUTION_STORE,
                ChatArchitectureSignal.DURABLE_DELIVERY_LEDGER,
            }
        ),
        forbidden_signals=frozenset(
            {ChatArchitectureSignal.ADAPTER_LOCAL_DELIVERY_RETRY_POLICY}
        ),
        target="Recovery is possible from durable orchestration rows plus adapter cursors.",
    ),
    ChatArchitectureCriterion(
        criterion_id="surface_as_projection",
        dimension=ChatArchitectureDimension.SURFACE_PLUGGABILITY,
        weight=8,
        required_signals=frozenset(
            {
                ChatArchitectureSignal.PROTOCOL_NEUTRAL_SURFACE_IDENTITY,
                ChatArchitectureSignal.SNAPSHOT_AND_EVENT_READ_MODELS,
            }
        ),
        forbidden_signals=frozenset(
            {ChatArchitectureSignal.SURFACE_LOCAL_TRANSCRIPT_ORDERING}
        ),
        target="A new surface binds identity and consumes snapshots/events.",
    ),
    ChatArchitectureCriterion(
        criterion_id="agent_as_capability_provider",
        dimension=ChatArchitectureDimension.AGENT_PLUGGABILITY,
        weight=8,
        required_signals=frozenset(
            {ChatArchitectureSignal.PROTOCOL_NEUTRAL_AGENT_REGISTRY}
        ),
        forbidden_signals=frozenset(
            {ChatArchitectureSignal.AGENT_SPECIFIC_ROUTE_BRANCHING}
        ),
        target="A new agent supplies a descriptor/harness without route-specific branching.",
    ),
    ChatArchitectureCriterion(
        criterion_id="pure_policy_functions",
        dimension=ChatArchitectureDimension.TESTABILITY,
        weight=7,
        required_signals=frozenset(
            {ChatArchitectureSignal.PURE_RECOVERY_AND_PROJECTION_FUNCTIONS}
        ),
        forbidden_signals=frozenset(
            {ChatArchitectureSignal.OPAQUE_STATUS_STRING_MAPPING}
        ),
        target="Policy and mapping decisions are pure functions with table-driven tests.",
    ),
    ChatArchitectureCriterion(
        criterion_id="observable_repairable_read_models",
        dimension=ChatArchitectureDimension.OBSERVABILITY,
        weight=6,
        required_signals=frozenset(
            {
                ChatArchitectureSignal.SNAPSHOT_AND_EVENT_READ_MODELS,
                ChatArchitectureSignal.DURABLE_OPERATION_LEDGER,
            }
        ),
        target="Streams are invalidation paths; snapshots repair gaps and expose cursors.",
    ),
)


CURRENT_CHAT_ARCHITECTURE_SIGNALS: frozenset[ChatArchitectureSignal] = frozenset(
    {
        ChatArchitectureSignal.CANONICAL_OPERATION_STATE_MACHINE,
        ChatArchitectureSignal.CANONICAL_THREAD_STATUS_MACHINE,
        ChatArchitectureSignal.CANONICAL_TURN_LIFECYCLE_CONTRACT,
        ChatArchitectureSignal.DURABLE_OPERATION_LEDGER,
        ChatArchitectureSignal.DURABLE_THREAD_EXECUTION_STORE,
        ChatArchitectureSignal.DURABLE_DELIVERY_LEDGER,
        ChatArchitectureSignal.PROTOCOL_NEUTRAL_SURFACE_IDENTITY,
        ChatArchitectureSignal.PROTOCOL_NEUTRAL_AGENT_REGISTRY,
        ChatArchitectureSignal.PURE_RECOVERY_AND_PROJECTION_FUNCTIONS,
        ChatArchitectureSignal.SNAPSHOT_AND_EVENT_READ_MODELS,
        # Remaining PMA compatibility state and adapter-local cursors are known
        # migration pressure, not the target architecture.
        ChatArchitectureSignal.SURFACE_LOCAL_LIFECYCLE_MACHINE,
        ChatArchitectureSignal.OPAQUE_STATUS_STRING_MAPPING,
    }
)


def normalize_chat_architecture_signals(
    signals: Iterable[ChatArchitectureSignal | str],
) -> frozenset[ChatArchitectureSignal]:
    normalized: set[ChatArchitectureSignal] = set()
    for signal in signals:
        if isinstance(signal, ChatArchitectureSignal):
            normalized.add(signal)
            continue
        normalized.add(ChatArchitectureSignal(str(signal).strip()))
    return frozenset(normalized)


def evaluate_chat_architecture_goal(
    signals: Iterable[ChatArchitectureSignal | str],
    *,
    criteria: Sequence[ChatArchitectureCriterion] = CHAT_ARCHITECTURE_GOAL_CRITERIA,
) -> ChatArchitectureGoalEvaluation:
    """Score chat architecture evidence against the target architecture."""

    normalized_signals = normalize_chat_architecture_signals(signals)
    findings: list[ChatArchitectureFinding] = []
    score = 0
    max_score = 0
    for criterion in criteria:
        max_score += criterion.weight
        satisfied = criterion.satisfied_by(normalized_signals)
        if satisfied:
            score += criterion.weight
        findings.append(
            ChatArchitectureFinding(
                criterion_id=criterion.criterion_id,
                dimension=criterion.dimension,
                weight=criterion.weight,
                satisfied=satisfied,
                target=criterion.target,
                missing_signals=criterion.missing_signals(normalized_signals),
                blocking_signals=criterion.blocking_signals(normalized_signals),
            )
        )
    return ChatArchitectureGoalEvaluation(
        score=score,
        max_score=max_score,
        findings=tuple(findings),
        signals=normalized_signals,
    )


def current_chat_architecture_goal_evaluation() -> ChatArchitectureGoalEvaluation:
    """Return the documented current-state evaluation for review and tickets."""

    return evaluate_chat_architecture_goal(CURRENT_CHAT_ARCHITECTURE_SIGNALS)


def chat_architecture_goal_summary(
    signals: Iterable[ChatArchitectureSignal | str],
) -> Mapping[str, object]:
    """Return a stable, JSON-friendly summary for diagnostics."""

    return evaluate_chat_architecture_goal(signals).to_dict()


__all__ = [
    "CHAT_ARCHITECTURE_GOAL_CRITERIA",
    "CURRENT_CHAT_ARCHITECTURE_SIGNALS",
    "NEGATIVE_SIGNALS",
    "POSITIVE_SIGNALS",
    "ChatArchitectureCriterion",
    "ChatArchitectureDimension",
    "ChatArchitectureFinding",
    "ChatArchitectureGoalEvaluation",
    "ChatArchitectureSignal",
    "chat_architecture_goal_summary",
    "current_chat_architecture_goal_evaluation",
    "evaluate_chat_architecture_goal",
    "normalize_chat_architecture_signals",
]
