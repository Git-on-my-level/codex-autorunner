from codex_autorunner.core.flows import (
    FLOW_ACTION_NAMES,
    FLOW_ACTION_SPECS,
    FLOW_ACTIONS_WITH_RUN_PICKER,
    flow_action_summary,
    flow_help_lines,
    normalize_flow_action,
)


def test_flow_action_catalog_has_expected_order() -> None:
    assert FLOW_ACTION_NAMES == tuple(spec.name for spec in FLOW_ACTION_SPECS)
    assert [spec.name for spec in FLOW_ACTION_SPECS] == [
        "status",
        "runs",
        "issue",
        "plan",
        "start",
        "restart",
        "resume",
        "stop",
        "archive",
        "recover",
        "reply",
    ]


def test_flow_action_catalog_marks_picker_actions() -> None:
    assert FLOW_ACTIONS_WITH_RUN_PICKER == frozenset(
        {"status", "restart", "resume", "stop", "archive", "recover", "reply"}
    )


def test_flow_help_lines_support_surface_overrides() -> None:
    lines = flow_help_lines(
        prefix="/flow",
        usage_overrides={"start": "[--force-new]", "reply": "<message>"},
    )
    assert lines[0] == "Flow commands:"
    assert "/flow start [--force-new]" in lines
    assert "/flow reply <message>" in lines


def test_flow_action_summary_is_comma_separated() -> None:
    assert flow_action_summary() == ", ".join(FLOW_ACTION_NAMES)


def test_normalize_flow_action_aliases_bootstrap_to_start() -> None:
    assert normalize_flow_action("bootstrap") == "start"
