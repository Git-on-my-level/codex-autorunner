from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.chat_surface_integration.harness import patch_hermes_runtime
from tests.chat_surface_lab.scenario_runner import (
    ChatSurfaceScenarioRunner,
    load_scenario_by_id,
)


@pytest.mark.anyio
async def test_runner_executes_first_visible_feedback_and_emits_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_scenario_by_id("first_visible_feedback")
    runner = ChatSurfaceScenarioRunner(
        output_root=tmp_path / "scenario-runs",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
    )

    result = await runner.run_scenario(scenario)
    assert result.skipped is False
    assert result.summary_path is not None and result.summary_path.exists()
    assert len(result.surface_results) == 2
    assert len(result.observed_budgets) == 4
    for surface in result.surface_results:
        assert surface.execution_status == "ok"
        assert surface.transcript.events
        artifact_paths = [record.path for record in surface.artifact_manifest.artifacts]
        assert artifact_paths
        for path in artifact_paths:
            assert path.exists()

    summary_payload = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary_payload["scenario_id"] == "first_visible_feedback"
    assert summary_payload["observed_budgets"]


@pytest.mark.anyio
async def test_runner_executes_queued_visibility_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_scenario_by_id("queued_visibility")
    runner = ChatSurfaceScenarioRunner(
        output_root=tmp_path / "scenario-runs",
        apply_runtime_patch=lambda runtime: patch_hermes_runtime(monkeypatch, runtime),
    )

    result = await runner.run_scenario(scenario)
    assert result.skipped is False
    assert len(result.surface_results) == 1
    discord = result.surface_results[0]
    assert discord.surface.value == "discord"
    assert discord.execution_status == "interrupted"
    assert any(
        budget.budget_id == "queue_visible" for budget in result.observed_budgets
    )


@pytest.mark.anyio
async def test_runner_handles_reference_only_scenarios_without_surface_execution(
    tmp_path: Path,
) -> None:
    scenario = load_scenario_by_id("restart_recovery")
    runner = ChatSurfaceScenarioRunner(output_root=tmp_path / "scenario-runs")

    result = await runner.run_scenario(scenario)
    assert result.skipped is True
    assert result.surface_results == ()
    assert result.summary_path is not None and result.summary_path.exists()
