from __future__ import annotations

from codex_autorunner.core.validation_lanes import (
    classify_changed_files,
    lane_selection_to_payload,
    render_lane_selection,
)


def test_classify_core_lane_for_core_paths() -> None:
    selection = classify_changed_files(["src/codex_autorunner/core/update_targets.py"])

    assert selection.lane == "core"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("core",)


def test_classify_web_ui_lane_for_web_paths() -> None:
    selection = classify_changed_files(
        ["src/codex_autorunner/static_src/updateTargets.ts"]
    )

    assert selection.lane == "web-ui"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("web-ui",)


def test_classify_chat_apps_lane_for_chat_paths() -> None:
    selection = classify_changed_files(["tests/test_telegram_flow_status.py"])

    assert selection.lane == "chat-apps"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("chat-apps",)


def test_classify_core_lane_for_root_level_backend_test() -> None:
    selection = classify_changed_files(["tests/test_hub_supervisor.py"])

    assert selection.lane == "core"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("core",)


def test_classify_web_ui_lane_for_root_level_web_test() -> None:
    selection = classify_changed_files(["tests/test_app_server_events.py"])

    assert selection.lane == "web-ui"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("web-ui",)
    assert selection.lane_paths == (("web-ui", ("tests/test_app_server_events.py",)),)


def test_classify_web_ui_lane_for_ticket_flow_ui_integration_test() -> None:
    selection = classify_changed_files(["tests/test_ticket_flow_ui_integration.py"])

    assert selection.lane == "web-ui"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("web-ui",)
    assert selection.lane_paths == (
        ("web-ui", ("tests/test_ticket_flow_ui_integration.py",)),
    )


def test_classify_web_ui_lane_for_root_level_voice_ui_test() -> None:
    selection = classify_changed_files(["tests/test_voice_ui.py"])

    assert selection.lane == "web-ui"
    assert selection.reason == "single-lane-diff"
    assert selection.lanes_touched == ("web-ui",)
    assert selection.lane_paths == (("web-ui", ("tests/test_voice_ui.py",)),)


def test_shared_risk_paths_force_aggregate() -> None:
    selection = classify_changed_files(["scripts/check.sh"])

    assert selection.lane == "aggregate"
    assert selection.reason == "shared-risk-path"
    assert selection.shared_risk_paths == ("scripts/check.sh",)


def test_shared_risk_config_file_forces_aggregate() -> None:
    selection = classify_changed_files(["Makefile", "pyproject.toml"])

    assert selection.lane == "aggregate"
    assert selection.reason == "shared-risk-path"
    assert selection.shared_risk_paths == ("Makefile", "pyproject.toml")


def test_shared_risk_workflow_forces_aggregate() -> None:
    selection = classify_changed_files([".github/workflows/ci.yml"])

    assert selection.lane == "aggregate"
    assert selection.reason == "shared-risk-path"
    assert selection.shared_risk_paths == (".github/workflows/ci.yml",)


def test_unknown_paths_force_aggregate() -> None:
    selection = classify_changed_files(["README.md"])

    assert selection.lane == "aggregate"
    assert selection.reason == "unknown-path"
    assert selection.unknown_paths == ("README.md",)


def test_multi_lane_diff_forces_aggregate() -> None:
    selection = classify_changed_files(
        [
            "src/codex_autorunner/core/update_targets.py",
            "src/codex_autorunner/static_src/updateTargets.ts",
        ]
    )

    assert selection.lane == "aggregate"
    assert selection.reason == "multi-lane-diff"
    assert selection.lanes_touched == ("core", "web-ui")
    assert selection.lane_paths == (
        ("core", ("src/codex_autorunner/core/update_targets.py",)),
        ("web-ui", ("src/codex_autorunner/static_src/updateTargets.ts",)),
    )


def test_classification_is_deterministic_for_same_input_set() -> None:
    first = classify_changed_files(
        [
            "src/codex_autorunner/static_src/updateTargets.ts",
            "src/codex_autorunner/core/update_targets.py",
            "src/codex_autorunner/core/update_targets.py",
        ]
    )
    second = classify_changed_files(
        [
            "src/codex_autorunner/core/update_targets.py",
            "src/codex_autorunner/static_src/updateTargets.ts",
        ]
    )

    assert first == second


def test_windows_style_paths_are_normalized_before_classification() -> None:
    selection = classify_changed_files([".\\src\\codex_autorunner\\core\\runtime.py"])

    assert selection.lane == "core"
    assert selection.normalized_paths == ("src/codex_autorunner/core/runtime.py",)


def test_empty_path_set_defaults_to_aggregate() -> None:
    selection = classify_changed_files([])

    assert selection.lane == "aggregate"
    assert selection.reason == "empty-change-set"


def test_payload_and_human_rendering_are_stable() -> None:
    selection = classify_changed_files(
        [
            "src/codex_autorunner/core/update_targets.py",
            "scripts/check.sh",
        ]
    )

    assert lane_selection_to_payload(selection) == {
        "lane": "aggregate",
        "reason": "shared-risk-path",
        "paths": [
            "scripts/check.sh",
            "src/codex_autorunner/core/update_targets.py",
        ],
        "lanes_touched": ["core"],
        "lane_paths": {"core": ["src/codex_autorunner/core/update_targets.py"]},
        "shared_risk_paths": ["scripts/check.sh"],
        "unknown_paths": [],
    }
    assert render_lane_selection(selection) == "\n".join(
        [
            "lane: aggregate",
            "reason: shared-risk-path",
            "changed_files: 2",
            "lanes_touched: core",
            "core_paths: src/codex_autorunner/core/update_targets.py",
            "shared_risk_paths: scripts/check.sh",
        ]
    )
