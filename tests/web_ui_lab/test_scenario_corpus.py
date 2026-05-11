from __future__ import annotations

from tests.web_ui_lab.corpus import SCRIPT_ROUTE_PACK, WEB_UI_SCENARIOS
from tests.web_ui_lab.scenario_models import ScenarioTag, SeedFixtureKind


def test_scenario_ids_are_unique_and_stable() -> None:
    ids = [scenario.scenario_id for scenario in WEB_UI_SCENARIOS]
    assert len(ids) == len(set(ids))
    for scenario_id in ids:
        assert scenario_id == scenario_id.strip()
        assert scenario_id.lower() == scenario_id
        assert " " not in scenario_id


def test_scenarios_are_well_formed() -> None:
    assert WEB_UI_SCENARIOS
    for scenario in WEB_UI_SCENARIOS:
        assert scenario.title
        assert scenario.route_name in SCRIPT_ROUTE_PACK
        assert scenario.route_path == SCRIPT_ROUTE_PACK[scenario.route_name]
        assert scenario.route_path.startswith("/")
        assert scenario.viewports
        assert scenario.screen.visible_landmarks
        assert scenario.screen.loading_markers
        assert scenario.read_models.fixture_kind == scenario.seed_fixture
        assert scenario.tags
        assert scenario.evidence.screenshot.endswith("screenshot.png")
        assert scenario.evidence.accessibility_snapshot.endswith("a11y_snapshot.json")
        assert scenario.evidence.dom_summary.endswith("dom_summary.json")
        assert scenario.evidence.console_log.endswith("console.log")
        assert scenario.evidence.network_failures.endswith("network_failures.json")
        assert scenario.evidence.layout_diagnostics.endswith("layout_diagnostics.json")


def test_corpus_covers_script_route_pack() -> None:
    covered_routes = {scenario.route_name for scenario in WEB_UI_SCENARIOS}
    missing = set(SCRIPT_ROUTE_PACK).difference(covered_routes)
    assert not missing, f"web_ui_screens.py routes missing from corpus: {missing}"


def test_critical_coverage_spans_core_user_surfaces() -> None:
    critical = {
        scenario.route_name
        for scenario in WEB_UI_SCENARIOS
        if ScenarioTag.CRITICAL in scenario.tags
    }
    required = {
        "chat",
        "hub",
        "repos",
        "repo-detail",
        "repo-tickets",
        "worktree-detail",
        "worktree-tickets",
        "ticket-detail",
    }
    missing = required.difference(critical)
    assert not missing, f"critical Web UI route coverage missing: {missing}"


def test_high_signal_seed_states_are_present() -> None:
    fixture_kinds = {scenario.seed_fixture for scenario in WEB_UI_SCENARIOS}
    required = {
        SeedFixtureKind.EMPTY_HUB,
        SeedFixtureKind.SEEDED_REPO_WORKTREE_TICKET,
        SeedFixtureKind.CHAT_LIST_DETAIL,
        SeedFixtureKind.LARGE_LIST_WINDOWING,
        SeedFixtureKind.PMA_PENDING,
        SeedFixtureKind.PMA_RUNNING,
        SeedFixtureKind.PMA_FINAL,
    }
    missing = required.difference(fixture_kinds)
    assert not missing, f"high-signal fixture coverage missing: {missing}"


def test_scenarios_reference_existing_route_and_read_model_vocabulary() -> None:
    for scenario in WEB_UI_SCENARIOS:
        referenced_routes = (
            scenario.read_models.read_model_routes + scenario.read_models.api_routes
        )
        assert referenced_routes or scenario.read_models.frontend_mappers
        for route in referenced_routes:
            assert route.startswith(("/hub/", "/api/", "/repos/")), (
                scenario.scenario_id,
                route,
            )
        for mapper in scenario.read_models.frontend_mappers:
            assert mapper.endswith((".ts", ".svelte")), (scenario.scenario_id, mapper)


def test_browser_scenarios_have_desktop_and_mobile_viewports() -> None:
    for scenario in WEB_UI_SCENARIOS:
        if ScenarioTag.BROWSER not in scenario.tags:
            continue
        labels = {viewport.artifact_label for viewport in scenario.viewports}
        assert labels == {"1440x1000", "390x844"}, scenario.scenario_id
