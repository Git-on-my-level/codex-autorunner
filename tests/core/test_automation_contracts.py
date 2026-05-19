from __future__ import annotations

import pytest

from codex_autorunner.core.automation import (
    AutomationContractError,
    AutomationRule,
    AutomationSchedule,
)
from codex_autorunner.core.automation.models import (
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PMA_TURN,
    EXECUTOR_PUBLISH_OPERATION,
    EXECUTOR_TICKET_FLOW,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONE_SHOT,
    SCHEDULE_WEEKLY,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_MANUAL,
    TRIGGER_KIND_SCHEDULE,
)


def test_canonical_automation_contracts_cover_supported_rule_shapes() -> None:
    event_rule = AutomationRule.create(
        name="Event managed turn",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["scm.github.pull_request.opened"]},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
        executor={"prompt_template": "Review {{ event.repo_id }}"},
    )
    manual_rule = AutomationRule.create(
        name="Manual PMA turn",
        trigger_kind=TRIGGER_KIND_MANUAL,
        trigger={"event_types": ["manual.run"]},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_PMA_TURN,
        executor={"message": "Wake {{ event.payload.prompt }}"},
    )
    ticket_flow_rule = AutomationRule.create(
        name="Weekly ticket flow",
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"schedule_kind": SCHEDULE_WEEKLY},
        target_policy=TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        target={"base_repo_id": "repo-1"},
        executor_kind=EXECUTOR_TICKET_FLOW,
        executor={"ticket_pack": {"source": "inline", "tickets": []}},
    )
    publish_rule = AutomationRule.create(
        name="Publish operation",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["scm.github.workflow_run.completed"]},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_PUBLISH_OPERATION,
        executor={
            "operation_kind": "notify_chat",
            "payload": {"message": "complete"},
        },
    )

    assert event_rule.executor["prompt_template"] == "Review {{ event.repo_id }}"
    assert manual_rule.trigger["event_types"] == ["manual.run"]
    assert ticket_flow_rule.trigger["schedule_kind"] == SCHEDULE_WEEKLY
    assert publish_rule.executor["operation_kind"] == "notify_chat"


def test_canonical_schedule_contracts_cover_supported_schedule_shapes() -> None:
    one_shot = AutomationSchedule.create(
        rule_id="rule-1",
        schedule_kind=SCHEDULE_ONE_SHOT,
        next_fire_at="2026-01-01T00:00:00Z",
    )
    interval = AutomationSchedule.create(
        rule_id="rule-1",
        schedule_kind=SCHEDULE_INTERVAL,
        next_fire_at="2026-01-01T00:00:00Z",
        schedule={"interval_seconds": "90"},
    )
    daily = AutomationSchedule.create(
        rule_id="rule-1",
        schedule_kind=SCHEDULE_DAILY,
        next_fire_at="2026-01-01T09:00:00Z",
        schedule={"hour": 9, "minute": 0},
    )
    weekly = AutomationSchedule.create(
        rule_id="rule-1",
        schedule_kind=SCHEDULE_WEEKLY,
        next_fire_at="2026-01-05T09:00:00Z",
        schedule={"weekdays": ["0"], "hour": 9, "minute": 0},
    )

    assert one_shot.next_fire_at == "2026-01-01T00:00:00Z"
    assert interval.schedule["interval_seconds"] == 90
    assert daily.schedule == {"hour": 9, "minute": 0}
    assert weekly.schedule["weekdays"] == [0]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {
                "trigger_kind": TRIGGER_KIND_EVENT,
                "trigger": {"event_type": "manual.run"},
                "target_policy": TARGET_POLICY_HUB,
                "executor_kind": EXECUTOR_PMA_TURN,
            },
            "AUTOMATION_CONTRACT_LEGACY_TRIGGER",
        ),
        (
            {
                "trigger_kind": TRIGGER_KIND_EVENT,
                "trigger": {"event_types": []},
                "target_policy": TARGET_POLICY_HUB,
                "executor_kind": EXECUTOR_PMA_TURN,
            },
            "AUTOMATION_CONTRACT_INVALID_LIST",
        ),
        (
            {
                "trigger_kind": TRIGGER_KIND_EVENT,
                "trigger": {"event_types": ["manual.run"]},
                "target_policy": TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
                "target": {"repo_id": "repo-1"},
                "executor_kind": EXECUTOR_TICKET_FLOW,
            },
            "AUTOMATION_CONTRACT_TARGET_REQUIRED",
        ),
        (
            {
                "trigger_kind": TRIGGER_KIND_EVENT,
                "trigger": {"event_types": ["manual.run"]},
                "target_policy": TARGET_POLICY_HUB,
                "executor_kind": EXECUTOR_TICKET_FLOW,
                "executor": {"ticket_pack": "legacy-pack"},
            },
            "AUTOMATION_CONTRACT_INVALID_TICKET_PACK",
        ),
    ],
)
def test_rule_contracts_fail_loudly_for_malformed_legacy_shapes(
    kwargs: dict, code: str
) -> None:
    with pytest.raises(AutomationContractError, match=code):
        AutomationRule.create(name="Bad rule", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {
                "rule_id": "rule-1",
                "schedule_kind": SCHEDULE_ONE_SHOT,
                "schedule": {"due_at": "2026-01-01T00:00:00Z"},
            },
            "AUTOMATION_CONTRACT_LEGACY_SCHEDULE",
        ),
        (
            {
                "rule_id": "rule-1",
                "schedule_kind": SCHEDULE_INTERVAL,
                "next_fire_at": "2026-01-01T00:00:00Z",
                "schedule": {"interval_seconds": 0},
            },
            "AUTOMATION_CONTRACT_INVALID_NUMBER",
        ),
        (
            {
                "rule_id": "rule-1",
                "schedule_kind": SCHEDULE_DAILY,
                "next_fire_at": "2026-01-01T00:00:00Z",
                "schedule": {"hour": 24, "minute": 0},
            },
            "AUTOMATION_CONTRACT_INVALID_NUMBER",
        ),
    ],
)
def test_schedule_contracts_fail_loudly_for_malformed_legacy_shapes(
    kwargs: dict, code: str
) -> None:
    with pytest.raises(AutomationContractError, match=code):
        AutomationSchedule.create(**kwargs)
