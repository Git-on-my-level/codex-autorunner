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
        FlowActionPolicySnapshot(status="paused", archive_mode="confirm", has_run=True)
    )

    assert _enabled(
        FlowActionPolicySnapshot(status="paused", archive_mode="confirm", has_run=True)
    ) == ["resume", "restart", "archive"]
    archive = next(action for action in actions if action.action == "archive")
    assert archive.requires_confirmation is True


def test_policy_running_healthy() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="running",
            worker_health_status="alive",
            archive_mode="blocked",
            has_run=True,
            has_open_tickets=True,
        )
    ) == ["stop", "refresh"]


def test_policy_running_unhealthy() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="running",
            worker_health_status="dead",
            archive_mode="blocked",
            has_run=True,
        )
    ) == ["stop", "recover", "refresh"]


def test_policy_terminal_completed() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(
            status="completed",
            archive_mode="ready",
            has_run=True,
            has_open_tickets=True,
        )
    ) == ["start", "restart", "archive", "refresh"]


def test_policy_terminal_failed() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(status="failed", archive_mode="ready", has_run=True)
    ) == ["restart", "archive", "refresh"]


def test_policy_stopped() -> None:
    assert _enabled(
        FlowActionPolicySnapshot(status="stopped", archive_mode="ready", has_run=True)
    ) == ["restart", "archive", "refresh"]


def test_policy_archived_blocked() -> None:
    actions = build_flow_action_policy(
        FlowActionPolicySnapshot(status="running", archive_mode="blocked", has_run=True)
    )
    archive = next(action for action in actions if action.action == "archive")

    assert archive.enabled is False
    assert archive.disabled_reason == "Archive is blocked while the run is active"


def test_policy_no_run() -> None:
    assert _enabled(FlowActionPolicySnapshot(has_run=False, has_open_tickets=True)) == [
        "start"
    ]
