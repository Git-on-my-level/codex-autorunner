from __future__ import annotations

import pytest

from codex_autorunner.core.automation import (
    AutomationContractError,
    AutomationRule,
    AutomationSchedule,
)
from codex_autorunner.core.automation.models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_INPUT_KINDS,
    EXECUTOR_KINDS,
    EXECUTOR_PMA_OPERATOR_TURN,
    EXECUTOR_PUBLISH_OPERATION,
    EXECUTOR_TICKET_FLOW,
    LEGACY_EXECUTOR_KINDS,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONE_SHOT,
    SCHEDULE_WEEKLY,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_MANUAL,
    TRIGGER_KIND_SCHEDULE,
    AutomationChildExecutionEdge,
    AutomationRuntimeContract,
)


def test_canonical_automation_contracts_cover_supported_rule_shapes() -> None:
    event_rule = AutomationRule.create(
        name="Event agent task turn",
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["scm.github.pull_request.opened"]},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_AGENT_TASK_TURN,
        executor={
            "prompt_template": "Review {{ event.repo_id }}",
            "requested_runtime": {
                "agent": "codex",
                "model": "gpt-5.5",
                "profile": "default",
                "reasoning": "medium",
                "approval_policy": "never",
                "sandbox_policy": "danger-full-access",
                "workspace_scope": {"kind": "hub"},
            },
        },
    )
    manual_rule = AutomationRule.create(
        name="Manual PMA operator turn",
        trigger_kind=TRIGGER_KIND_MANUAL,
        trigger={"event_types": ["manual.run"]},
        target_policy=TARGET_POLICY_HUB,
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
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
    assert event_rule.executor["requested_runtime"]["agent"] == "codex"
    assert manual_rule.trigger["event_types"] == ["manual.run"]
    assert ticket_flow_rule.trigger["schedule_kind"] == SCHEDULE_WEEKLY
    assert publish_rule.executor["operation_kind"] == "notify_chat"


def test_executor_kind_sets_separate_product_modes_from_legacy_inputs() -> None:
    assert EXECUTOR_AGENT_TASK_TURN in EXECUTOR_KINDS
    assert EXECUTOR_PMA_OPERATOR_TURN in EXECUTOR_KINDS
    assert EXECUTOR_TICKET_FLOW in EXECUTOR_KINDS
    assert EXECUTOR_PUBLISH_OPERATION in EXECUTOR_KINDS
    assert LEGACY_EXECUTOR_MANAGED_THREAD_TURN not in EXECUTOR_KINDS
    assert LEGACY_EXECUTOR_MANAGED_THREAD_TURN in LEGACY_EXECUTOR_KINDS
    assert LEGACY_EXECUTOR_MANAGED_THREAD_TURN not in EXECUTOR_INPUT_KINDS


def test_runtime_contract_and_child_edge_serialize_canonical_shape() -> None:
    requested = AutomationRuntimeContract.from_dict(
        {
            "agent": "opencode",
            "model": "zai-coding-plan/glm-5.1",
            "profile": "security",
            "reasoning": "high",
            "approval_policy": "never",
            "sandbox_policy": "danger-full-access",
            "prompt_ref": {"kind": "inline", "sha256": "prompt-sha"},
            "input_ref": {"kind": "automation_event", "event_id": "event-1"},
            "workspace_scope": {"repo_id": "repo-1", "worktree_policy": "hub"},
        }
    )
    actual = AutomationRuntimeContract.from_dict(
        {
            "agent": "opencode",
            "model": "zai-coding-plan/glm-5.1",
            "profile": "security",
            "reasoning": "high",
            "backend_runtime_id": "backend-1",
            "provider_payload": {"provider": "zai"},
        }
    )

    edge = AutomationChildExecutionEdge.create(
        edge_id="edge-1",
        parent_job_id="job-1",
        child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
        child_id="thread-1",
        requested_runtime=requested,
        actual_runtime=actual,
        terminal_mapping={"succeeded": "succeeded", "failed": "failed"},
        created_at="2026-01-01T00:00:00Z",
    )

    serialized = edge.to_dict()

    assert serialized["authoritative_for_parent_completion"] is True
    assert serialized["requested_runtime"]["agent"] == "opencode"
    assert serialized["actual_runtime"]["backend_runtime_id"] == "backend-1"
    assert serialized["terminal_mapping"] == {
        "succeeded": "succeeded",
        "failed": "failed",
    }


def test_contracts_reject_unknown_new_mode_and_child_kind() -> None:
    with pytest.raises(ValueError, match="executor_kind must be one of"):
        AutomationRule.create(
            name="Unknown mode",
            trigger_kind=TRIGGER_KIND_MANUAL,
            target_policy=TARGET_POLICY_HUB,
            executor_kind="agent_turn",
        )
    with pytest.raises(ValueError, match="child_kind must be one of"):
        AutomationChildExecutionEdge.create(
            parent_job_id="job-1",
            child_kind="managed_thread",
            child_id="child-1",
            requested_runtime={},
        )


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
                "executor_kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
            },
            "AUTOMATION_CONTRACT_LEGACY_EXECUTOR_KIND",
        ),
        (
            {
                "trigger_kind": TRIGGER_KIND_EVENT,
                "trigger": {"event_types": []},
                "target_policy": TARGET_POLICY_HUB,
                "executor_kind": LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
            },
            "AUTOMATION_CONTRACT_LEGACY_EXECUTOR_KIND",
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
