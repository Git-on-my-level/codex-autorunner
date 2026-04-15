"""Declarative coverage contract for chat UX regression scenarios.

The contract keeps the required regression matrix explicit so test suites,
parity checks, and `car doctor` can fail loudly when a high-signal scenario is
removed or stops referencing concrete test coverage.
"""

from __future__ import annotations

from dataclasses import dataclass

REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS = (
    "fast_ack",
    "first_visible_feedback",
    "queued_visibility",
    "progress_anchor_reuse",
    "interrupt_optimistic_acceptance",
    "interrupt_confirmation",
    "restart_recovery",
    "duplicate_delivery",
)

REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS = (
    "first_visible_feedback",
    "queue_visible",
    "first_semantic_progress",
    "interrupt_visible",
)


@dataclass(frozen=True)
class ChatUxLatencyBudgetEntry:
    id: str
    description: str
    max_ms: float


@dataclass(frozen=True)
class ChatUxRegressionScenarioEntry:
    id: str
    description: str
    test_paths: tuple[str, ...]
    required_surfaces: tuple[str, ...] = ()
    latency_budget_ids: tuple[str, ...] = ()


CHAT_UX_LATENCY_BUDGETS = (
    ChatUxLatencyBudgetEntry(
        id="first_visible_feedback",
        description="First visible feedback must appear quickly enough that the turn does not feel dead.",
        max_ms=1500.0,
    ),
    ChatUxLatencyBudgetEntry(
        id="queue_visible",
        description="Queued turns must become visibly queued before users assume the submission failed.",
        max_ms=1500.0,
    ),
    ChatUxLatencyBudgetEntry(
        id="first_semantic_progress",
        description="Long-running turns should emit meaningful progress before they appear hung.",
        max_ms=5000.0,
    ),
    ChatUxLatencyBudgetEntry(
        id="interrupt_visible",
        description="Surface interrupt affordances must acknowledge the click or callback quickly.",
        max_ms=1500.0,
    ),
)


CHAT_UX_REGRESSION_CONTRACT = (
    ChatUxRegressionScenarioEntry(
        id="fast_ack",
        description="Interactive controls acknowledge immediately instead of looking dead.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
        ),
        required_surfaces=("discord", "telegram"),
        latency_budget_ids=("interrupt_visible",),
    ),
    ChatUxRegressionScenarioEntry(
        id="first_visible_feedback",
        description="Long-running turns render a visible placeholder quickly on both chat surfaces.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
        ),
        required_surfaces=("discord", "telegram"),
        latency_budget_ids=("first_visible_feedback", "first_semantic_progress"),
    ),
    ChatUxRegressionScenarioEntry(
        id="queued_visibility",
        description="Busy-thread queueing becomes visible before the user retries or abandons the action.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
        ),
        latency_budget_ids=("queue_visible",),
    ),
    ChatUxRegressionScenarioEntry(
        id="progress_anchor_reuse",
        description="Surface progress stays anchored to a stable placeholder instead of spawning duplicate progress messages.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
        ),
        required_surfaces=("discord", "telegram"),
    ),
    ChatUxRegressionScenarioEntry(
        id="interrupt_optimistic_acceptance",
        description="Interrupt controls optimistically acknowledge the action before backend completion.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
        ),
        required_surfaces=("discord", "telegram"),
        latency_budget_ids=("interrupt_visible",),
    ),
    ChatUxRegressionScenarioEntry(
        id="interrupt_confirmation",
        description="Interrupt requests reconcile to a durable interrupted state on both surfaces.",
        test_paths=(
            "tests/chat_surface_integration/test_hermes_pma_ux_regressions.py",
            "tests/chat_surface_integration/test_hermes_pma_surface_parity.py",
        ),
        required_surfaces=("discord", "telegram"),
    ),
    ChatUxRegressionScenarioEntry(
        id="restart_recovery",
        description="Managed-thread recovery after restart preserves queued work instead of silently dropping it.",
        test_paths=(
            "tests/surfaces/web/routes/pma_routes/test_managed_thread_runtime.py",
        ),
    ),
    ChatUxRegressionScenarioEntry(
        id="duplicate_delivery",
        description="Duplicate surface deliveries are deduped instead of replaying the same turn twice.",
        test_paths=(
            "tests/core/orchestration/test_chat_operation_ledger.py",
            "tests/integrations/discord/test_reliability.py",
            "tests/test_telegram_update_dedupe.py",
        ),
        required_surfaces=("discord", "telegram"),
    ),
)


__all__ = [
    "CHAT_UX_LATENCY_BUDGETS",
    "CHAT_UX_REGRESSION_CONTRACT",
    "ChatUxLatencyBudgetEntry",
    "ChatUxRegressionScenarioEntry",
    "REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS",
    "REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS",
]
