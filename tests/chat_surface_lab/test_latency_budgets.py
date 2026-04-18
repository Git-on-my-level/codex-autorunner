from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.chat_surface_integration.harness import patch_hermes_runtime
from tests.chat_surface_lab.latency_budget_runner import (
    DEFAULT_LATENCY_SCENARIO_IDS,
    run_chat_surface_latency_budget_suite,
)
from tests.chat_surface_lab.scenario_runner import ScenarioDefinition


@pytest.mark.anyio
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
