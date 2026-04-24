from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codex_autorunner.integrations.chat.ux_regression_contract import (
    REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS,
    campaign_north_star_status,
    format_campaign_scorecard,
)
from tests.chat_surface_integration.harness import patch_hermes_runtime
from tests.chat_surface_lab.latency_budget_runner import (
    DEFAULT_LATENCY_SCENARIO_IDS,
    run_chat_surface_latency_budget_suite,
)
from tests.chat_surface_lab.scenario_runner import ScenarioDefinition

# These assertions intentionally exercise the full latency-budget campaign suite.
# Healthy runs already take tens of seconds, so they belong on the explicit slow
# lane instead of the repo's default fast pre-commit path.


@pytest.mark.anyio
@pytest.mark.slow
async def test_latency_budget_suite_writes_artifacts_and_covers_required_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run_chat_surface_latency_budget_suite(
        artifact_dir=tmp_path / "diagnostics" / "chat-latency-budgets",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
    )

    assert result.passed is True
    assert result.latest_path.exists()
    assert result.history_path.exists()
    assert result.run_report_path.exists()

    payload = result.payload
    assert payload["scenario_ids"] == list(DEFAULT_LATENCY_SCENARIO_IDS)
    assert payload["signoff"]["passed"] is True
    assert payload["failures"] == []

    observed_budget_ids = {
        str(item["budget_id"]) for item in payload.get("observed_budgets", [])
    }
    for required_budget_id in (
        "first_visible_feedback",
        "queue_visible",
        "first_semantic_progress",
        "interrupt_visible",
    ):
        assert required_budget_id in observed_budget_ids

    latest_payload = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest_payload["run_id"] == result.run_id
    assert latest_payload["signoff"]["passed"] is True


@pytest.mark.anyio
@pytest.mark.slow
async def test_latency_budget_suite_failure_includes_scenario_and_budget_for_triage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _force_tight_budget(scenario: ScenarioDefinition) -> ScenarioDefinition:
        if scenario.scenario_id != "first_visible_feedback":
            return scenario
        adjusted = tuple(
            (
                replace(assertion, max_ms=0.0)
                if assertion.budget_id == "first_visible_feedback"
                else assertion
            )
            for assertion in scenario.latency_budgets
        )
        return replace(
            scenario,
            latency_budgets=adjusted,
        )

    result = await run_chat_surface_latency_budget_suite(
        scenario_ids=["first_visible_feedback"],
        artifact_dir=tmp_path / "diagnostics" / "chat-latency-budgets",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
        scenario_mutator=_force_tight_budget,
    )

    assert result.passed is False
    failures = result.payload.get("failures", [])
    assert failures
    assert any(
        item.get("scenario_id") == "first_visible_feedback" for item in failures
    ), failures
    assert any(
        "first_visible_feedback" in str(item.get("message", "")) for item in failures
    ), failures


@pytest.mark.anyio
@pytest.mark.slow
async def test_suite_report_includes_campaign_north_star_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run_chat_surface_latency_budget_suite(
        artifact_dir=tmp_path / "diagnostics" / "chat-latency-budgets",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
    )

    north_star = result.payload.get("campaign_north_star", {})
    assert isinstance(north_star, dict)

    budget_statuses = north_star.get("budget_statuses", [])
    assert len(budget_statuses) == 4
    for bs in budget_statuses:
        assert bs["passed"] is True, bs
        assert bs["observed"] is True, bs

    covered = north_star.get("covered_scenario_ids", [])
    assert "first_visible_feedback" in covered
    assert "queued_visibility" in covered
    assert "interrupt_optimistic_acceptance" in covered
    assert "fast_ack" in covered
    assert "duplicate_delivery" in covered
    assert "interrupt_confirmation" in covered
    assert "progress_anchor_reuse" in covered
    assert "restart_recovery" in covered

    assert "required_scenario_ids" in result.payload
    assert "required_budget_ids" in result.payload

    from tests.chat_surface_lab.latency_budget_runner import format_suite_summary

    summary_text = format_suite_summary(result)
    for budget_id in (
        "first_visible_feedback",
        "queue_visible",
        "first_semantic_progress",
        "interrupt_visible",
    ):
        assert budget_id in summary_text, f"summary missing budget_id {budget_id}"
    for sid in (
        "first_visible_feedback",
        "queued_visibility",
        "interrupt_optimistic_acceptance",
    ):
        assert sid in summary_text, f"summary missing scenario_id {sid}"


def test_campaign_scorecard_function_produces_readable_output() -> None:
    status = campaign_north_star_status(
        observed_budgets=[
            {"budget_id": "first_visible_feedback", "observed_ms": 200.0},
            {"budget_id": "queue_visible", "observed_ms": 300.0},
            {"budget_id": "first_semantic_progress", "observed_ms": 1000.0},
            {"budget_id": "interrupt_visible", "observed_ms": 400.0},
        ],
        observed_scenario_ids=list(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS),
    )
    assert status.green is True
    scorecard = format_campaign_scorecard(status)
    assert "GREEN" in scorecard
    assert "first_visible_feedback" in scorecard
    assert "queue_visible" in scorecard
    assert "first_semantic_progress" in scorecard
    assert "interrupt_visible" in scorecard
    for sid in REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS:
        assert sid in scorecard, f"scorecard missing scenario {sid}"


def test_campaign_scorecard_shows_red_when_budget_exceeded() -> None:
    status = campaign_north_star_status(
        observed_budgets=[
            {"budget_id": "first_visible_feedback", "observed_ms": 9999.0},
            {"budget_id": "queue_visible", "observed_ms": 300.0},
            {"budget_id": "first_semantic_progress", "observed_ms": 1000.0},
            {"budget_id": "interrupt_visible", "observed_ms": 400.0},
        ],
        observed_scenario_ids=list(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS),
    )
    assert status.green is False
    scorecard = format_campaign_scorecard(status)
    assert "RED" in scorecard
    assert "FAIL" in scorecard
