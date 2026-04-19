"""Declarative coverage contract for chat UX regression scenarios.

The contract keeps the required regression matrix explicit so test suites,
parity checks, and `car doctor` can fail loudly when a high-signal scenario is
removed or stops referencing concrete test coverage.

Campaign tickets should use ``campaign_north_star_status`` and
``format_campaign_scorecard`` to decide whether the chat UX campaign north star
is green or red for the current required scenario IDs and latency thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

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

CAMPAIGN_NORTH_STAR_LATENCY_THRESHOLDS = (
    ("first_visible_feedback", 1500.0),
    ("queue_visible", 1500.0),
    ("first_semantic_progress", 5000.0),
    ("interrupt_visible", 1500.0),
)

CAMPAIGN_CRITICAL_SCENARIO_MATRIX = (
    ("first_visible_feedback", "first_visible_feedback", True),
    ("first_visible_feedback", "first_semantic_progress", True),
    ("queued_visibility", "queue_visible", True),
    ("progress_anchor_reuse", None, False),
    ("interrupt_optimistic_acceptance", "interrupt_visible", True),
    ("interrupt_confirmation", None, True),
    ("restart_recovery", None, False),
    ("duplicate_delivery", None, True),
)

CAMPAIGN_SCENARIO_ALIAS_COVERAGE = {
    "fast_ack": "interrupt_optimistic_acceptance",
}


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


@dataclass(frozen=True)
class CampaignNorthStarBudgetStatus:
    budget_id: str
    threshold_ms: float
    observed_ms: float | None
    passed: bool
    observed: bool


@dataclass(frozen=True)
class CampaignNorthStarStatus:
    green: bool
    budget_statuses: tuple[CampaignNorthStarBudgetStatus, ...]
    covered_scenario_ids: tuple[str, ...]
    missing_scenario_ids: tuple[str, ...]


_CHAT_UX_LATENCY_BUDGETS = (
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

CHAT_UX_LATENCY_BUDGETS = _CHAT_UX_LATENCY_BUDGETS

_THRESHOLD_MAP: dict[str, float] = {
    budget_id: threshold
    for budget_id, threshold in CAMPAIGN_NORTH_STAR_LATENCY_THRESHOLDS
}


def campaign_north_star_status(
    *,
    observed_budgets: Sequence[Mapping[str, Any]],
    observed_scenario_ids: Sequence[str],
) -> CampaignNorthStarStatus:
    best_by_budget: dict[str, float] = {}
    for item in observed_budgets:
        budget_id = str(item.get("budget_id") or "")
        observed_ms = item.get("observed_ms")
        if not budget_id or not isinstance(observed_ms, (int, float)):
            continue
        current = best_by_budget.get(budget_id)
        if current is None or float(observed_ms) < current:
            best_by_budget[budget_id] = float(observed_ms)

    budget_statuses: list[CampaignNorthStarBudgetStatus] = []
    all_budgets_passed = True
    for budget_id, threshold_ms in CAMPAIGN_NORTH_STAR_LATENCY_THRESHOLDS:
        observed_ms_val: float | None = best_by_budget.get(budget_id)
        observed = observed_ms_val is not None
        passed = observed_ms_val is not None and observed_ms_val <= threshold_ms
        if not passed:
            all_budgets_passed = False
        budget_statuses.append(
            CampaignNorthStarBudgetStatus(
                budget_id=budget_id,
                threshold_ms=threshold_ms,
                observed_ms=observed_ms_val,
                passed=passed,
                observed=observed,
            )
        )

    observed_set = set(observed_scenario_ids)
    for alias, target in CAMPAIGN_SCENARIO_ALIAS_COVERAGE.items():
        if target in observed_set:
            observed_set.add(alias)
    required_set = set(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS)
    covered = tuple(sorted(required_set.intersection(observed_set)))
    missing = tuple(sorted(required_set.difference(observed_set)))

    green = all_budgets_passed and not missing
    return CampaignNorthStarStatus(
        green=green,
        budget_statuses=tuple(budget_statuses),
        covered_scenario_ids=covered,
        missing_scenario_ids=missing,
    )


def format_campaign_scorecard(status: CampaignNorthStarStatus) -> str:
    lines = [
        "CAMPAIGN NORTH STAR SCORECARD",
        f"overall: {'GREEN' if status.green else 'RED'}",
        "",
        "latency budgets:",
    ]
    for bs in status.budget_statuses:
        if not bs.observed:
            lines.append(
                f"  {bs.budget_id}: NO_OBSERVATION (threshold <= {bs.threshold_ms:.0f} ms)"
            )
        elif bs.passed:
            lines.append(
                f"  {bs.budget_id}: PASS ({bs.observed_ms:.1f} ms <= {bs.threshold_ms:.0f} ms)"
            )
        else:
            lines.append(
                f"  {bs.budget_id}: FAIL ({bs.observed_ms:.1f} ms > {bs.threshold_ms:.0f} ms)"
            )
    lines.append("")
    lines.append("scenario coverage:")
    for sid in REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS:
        covered_marker = "COVERED" if sid in status.covered_scenario_ids else "MISSING"
        lines.append(f"  {sid}: {covered_marker}")
    if status.missing_scenario_ids:
        lines.append("")
        lines.append(f"missing scenarios: {', '.join(status.missing_scenario_ids)}")
    return "\n".join(lines)


# Keep explicit module-level references so dead-code heuristics treat the
# campaign scorecard helpers as intentional public contract surface.
_CHAT_UX_CONTRACT_PUBLIC_API = (
    CampaignNorthStarBudgetStatus,
    CampaignNorthStarStatus,
    campaign_north_star_status,
    format_campaign_scorecard,
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
        description="Managed-thread recovery after restart preserves queued work at the orchestration/runtime layer.",
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
    "CAMPAIGN_CRITICAL_SCENARIO_MATRIX",
    "CAMPAIGN_NORTH_STAR_LATENCY_THRESHOLDS",
    "CAMPAIGN_SCENARIO_ALIAS_COVERAGE",
    "CHAT_UX_LATENCY_BUDGETS",
    "CHAT_UX_REGRESSION_CONTRACT",
    "CampaignNorthStarBudgetStatus",
    "CampaignNorthStarStatus",
    "ChatUxLatencyBudgetEntry",
    "ChatUxRegressionScenarioEntry",
    "REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS",
    "REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS",
    "campaign_north_star_status",
    "format_campaign_scorecard",
]
