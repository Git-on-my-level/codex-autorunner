from __future__ import annotations

from codex_autorunner.integrations.chat.ux_regression_contract import (
    REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS,
    REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS,
)
from tests.chat_surface_lab.scenario_runner import (
    SCENARIO_CORPUS_ROOT,
    iter_scenario_files,
    load_scenario,
    load_scenario_by_id,
    load_scenario_corpus,
)

_REQUIRED_CORPUS_SCENARIOS = {
    "first_visible_feedback",
    "progress_anchor_reuse",
    "queued_visibility",
    "interrupt_optimistic_acceptance",
    "interrupt_confirmation",
    "timeout_when_terminal_missing",
    "duplicate_delivery",
    "restart_recovery",
}


def test_scenario_corpus_contains_required_high_signal_regressions() -> None:
    files = iter_scenario_files()
    assert files
    scenario_ids = {load_scenario(path).scenario_id for path in files}
    missing = _REQUIRED_CORPUS_SCENARIOS.difference(scenario_ids)
    assert not missing, missing


def test_scenario_corpus_links_to_regression_contract_where_applicable() -> None:
    contract_ids = set(REQUIRED_CHAT_UX_REGRESSION_SCENARIO_IDS)
    latency_budget_ids = set(REQUIRED_CHAT_UX_LATENCY_BUDGET_IDS)
    scenarios = load_scenario_corpus()
    assert scenarios

    linked_to_contract = []
    for scenario in scenarios:
        linked_ids = set(scenario.contract_links.regression_ids)
        assert linked_ids.issubset(contract_ids), (
            scenario.scenario_id,
            linked_ids.difference(contract_ids),
        )
        linked_budget_ids = set(scenario.contract_links.latency_budget_ids)
        assert linked_budget_ids.issubset(latency_budget_ids), (
            scenario.scenario_id,
            linked_budget_ids.difference(latency_budget_ids),
        )
        if linked_ids:
            linked_to_contract.append(scenario.scenario_id)
    assert len(linked_to_contract) >= 5


def test_scenario_loader_can_roundtrip_lookup_by_id() -> None:
    scenario = load_scenario_by_id("first_visible_feedback")
    assert scenario.scenario_id == "first_visible_feedback"
    assert scenario.runtime.scenario == "official"
    assert scenario.execution_mode == "surface_harness"


def test_reference_only_scenario_declares_supporting_reference_paths() -> None:
    scenario = load_scenario_by_id("restart_recovery")
    assert scenario.execution_mode == "reference_only"
    assert scenario.contract_links.references
    for reference in scenario.contract_links.references:
        assert reference.startswith("tests/")


def test_scenario_corpus_files_live_under_expected_root() -> None:
    files = iter_scenario_files()
    assert files
    for path in files:
        assert SCENARIO_CORPUS_ROOT in path.parents
