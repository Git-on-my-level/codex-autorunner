from codex_autorunner.core.flows.action_policy import (
    FlowActionPolicySnapshot,
    build_flow_action_policy,
)


def _enabled(snapshot: FlowActionPolicySnapshot) -> list[str]:
    return [
        descriptor.action
        for descriptor in build_flow_action_policy(snapshot)
        if descriptor.enabled
    ]


def test_policy_paused_run() -> None:
    actions = build_flow_action_policy(
        FlowActionPolicySnapshot(status="paused", retire_mode="confirm", has_run=True)
    )

    assert _enabled(
        FlowActionPolicySnapshot(status="paused", retire_mode="confirm", has_run=True)
    ) == ["resume", "restart", "retire"]
    retire = next(action for action in actions if action.action == "retire")
    assert retire.requires_confirmation is True


def test_policy_running_healthy() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="running",
            worker_health_status="alive",
            retire_mode="blocked",
            has_run=True,
            has_open_tickets=True,
        )
    ) == ["stop", "refresh"]


def test_policy_running_unhealthy() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="running",
            worker_health_status="dead",
            retire_mode="blocked",
            has_run=True,
        )
    ) == ["stop", "recover", "refresh"]


def test_policy_terminal_completed() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="completed",
            retire_mode="ready",
            has_run=True,
            has_open_tickets=True,
        )
    ) == ["start", "restart", "retire", "refresh"]


def test_policy_terminal_failed() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(status="failed", retire_mode="ready", has_run=True)
    ) == ["restart", "retire", "refresh"]


def test_policy_stopped() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(status="stopped", retire_mode="ready", has_run=True)
    ) == ["restart", "retire", "refresh"]


def test_policy_retired_blocked() -> None:
    actions = build_flow_action_policy(
        FlowActionPolicySnapshot(status="running", retire_mode="blocked", has_run=True)
    )
    retire = next(action for action in actions if action.action == "retire")

    assert retire.enabled is False
    assert retire.disabled_reason == "Retire is blocked while the run is active"


def test_policy_no_run() -> None:
    assert _enabled(FlowActionPolicySnapshot(has_run=False, has_open_tickets=True)) == [
        "start"
    ]
