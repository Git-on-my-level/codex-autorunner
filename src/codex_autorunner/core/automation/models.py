from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..text_utils import _normalize_text
from ..time_utils import now_iso

TRIGGER_KIND_EVENT = "event"
TRIGGER_KIND_SCHEDULE = "schedule"
TRIGGER_KIND_MANUAL = "manual"
TRIGGER_KINDS = frozenset(
    {TRIGGER_KIND_EVENT, TRIGGER_KIND_SCHEDULE, TRIGGER_KIND_MANUAL}
)

TARGET_POLICY_EXISTING_REPO = "existing_repo"
TARGET_POLICY_EXISTING_WORKTREE = "existing_worktree"
TARGET_POLICY_NEW_AUTOMATION_WORKTREE = "new_automation_worktree"
TARGET_POLICY_AUTO_WORKTREE = "auto_worktree"
TARGET_POLICY_PR_WORKTREE = "pr_worktree"
TARGET_POLICY_HUB = "hub"
TARGET_POLICIES = frozenset(
    {
        TARGET_POLICY_EXISTING_REPO,
        TARGET_POLICY_EXISTING_WORKTREE,
        TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        TARGET_POLICY_AUTO_WORKTREE,
        TARGET_POLICY_PR_WORKTREE,
        TARGET_POLICY_HUB,
    }
)

EXECUTOR_AGENT_TASK_TURN = "agent_task_turn"
EXECUTOR_PMA_OPERATOR_TURN = "pma_operator_turn"
EXECUTOR_TICKET_FLOW = "ticket_flow"
EXECUTOR_PUBLISH_CHAT_NOTIFICATION = "publish_chat_notification"
EXECUTOR_GITHUB_REACTION = "github_reaction"
EXECUTOR_GITHUB_COMMENT = "github_comment"
EXECUTOR_PUBLISH_OPERATION = "publish_operation"
LEGACY_EXECUTOR_MANAGED_THREAD_TURN = "managed_thread_turn"
LEGACY_EXECUTOR_PMA_TURN = "pma_turn"
EXECUTOR_MANAGED_THREAD_TURN = LEGACY_EXECUTOR_MANAGED_THREAD_TURN
EXECUTOR_KINDS = frozenset(
    {
        EXECUTOR_AGENT_TASK_TURN,
        EXECUTOR_PMA_OPERATOR_TURN,
        EXECUTOR_TICKET_FLOW,
        EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
        EXECUTOR_GITHUB_REACTION,
        EXECUTOR_GITHUB_COMMENT,
        EXECUTOR_PUBLISH_OPERATION,
    }
)
LEGACY_EXECUTOR_KINDS = frozenset(
    {LEGACY_EXECUTOR_MANAGED_THREAD_TURN, LEGACY_EXECUTOR_PMA_TURN}
)
EXECUTOR_INPUT_KINDS = EXECUTOR_KINDS | LEGACY_EXECUTOR_KINDS

AUTOMATION_CHILD_KIND_AGENT_TASK = "agent_task"
AUTOMATION_CHILD_KIND_PMA_OPERATOR = "pma_operator"
AUTOMATION_CHILD_KIND_TICKET_FLOW = "ticket_flow"
AUTOMATION_CHILD_KIND_PUBLISH_OPERATION = "publish_operation"
AUTOMATION_CHILD_KINDS = frozenset(
    {
        AUTOMATION_CHILD_KIND_AGENT_TASK,
        AUTOMATION_CHILD_KIND_PMA_OPERATOR,
        AUTOMATION_CHILD_KIND_TICKET_FLOW,
        AUTOMATION_CHILD_KIND_PUBLISH_OPERATION,
    }
)
AUTOMATION_CHILD_TERMINAL_MAPPING = {
    "succeeded": "succeeded",
    "failed": "failed",
    "policy_violated": "failed",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
}

JOB_PENDING = "pending"
JOB_CLAIMED = "claimed"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_SKIPPED = "skipped"
JOB_PAUSED = "paused"
JOB_DEAD_LETTERED = "dead_lettered"
JOB_STATES = frozenset(
    {
        JOB_PENDING,
        JOB_CLAIMED,
        JOB_RUNNING,
        JOB_SUCCEEDED,
        JOB_FAILED,
        JOB_CANCELLED,
        JOB_SKIPPED,
        JOB_PAUSED,
        JOB_DEAD_LETTERED,
    }
)
JOB_TERMINAL_STATES = frozenset(
    {JOB_SUCCEEDED, JOB_CANCELLED, JOB_SKIPPED, JOB_DEAD_LETTERED}
)
JOB_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    JOB_PENDING: frozenset({JOB_CLAIMED, JOB_RUNNING, JOB_CANCELLED, JOB_SKIPPED}),
    JOB_CLAIMED: frozenset({JOB_RUNNING, JOB_PENDING, JOB_CANCELLED, JOB_SKIPPED}),
    JOB_RUNNING: frozenset(
        {
            JOB_SUCCEEDED,
            JOB_FAILED,
            JOB_PAUSED,
            JOB_CANCELLED,
            JOB_SKIPPED,
            JOB_DEAD_LETTERED,
        }
    ),
    JOB_PAUSED: frozenset({JOB_PENDING, JOB_RUNNING, JOB_CANCELLED, JOB_DEAD_LETTERED}),
    JOB_FAILED: frozenset({JOB_PENDING, JOB_DEAD_LETTERED}),
}

SCHEDULE_ONE_SHOT = "one_shot"
SCHEDULE_INTERVAL = "interval"
SCHEDULE_DAILY = "daily"
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_KINDS = frozenset(
    {SCHEDULE_ONE_SHOT, SCHEDULE_INTERVAL, SCHEDULE_DAILY, SCHEDULE_WEEKLY}
)

APPROVAL_PAUSE_AND_REQUEST_USER = "pause_and_request_user"
APPROVAL_INHERIT_PROFILE = "inherit_profile"
APPROVAL_AUTO_DECLINE = "auto_decline"
APPROVAL_NEVER_REQUIRE_APPROVAL = "never_require_approval"
APPROVAL_MODES = frozenset(
    {
        APPROVAL_PAUSE_AND_REQUEST_USER,
        APPROVAL_INHERIT_PROFILE,
        APPROVAL_AUTO_DECLINE,
        APPROVAL_NEVER_REQUIRE_APPROVAL,
    }
)

AUTOMATION_EVENT_TYPES = frozenset(
    {
        "lifecycle.dispatch_created",
        "lifecycle.flow_started",
        "lifecycle.flow_resumed",
        "lifecycle.flow_paused",
        "lifecycle.flow_completed",
        "lifecycle.flow_failed",
        "lifecycle.flow_stopped",
        "scm.github.pull_request.opened",
        "scm.github.pull_request.closed",
        "scm.github.pull_request_review.submitted",
        "scm.github.pull_request_review_comment.created",
        "scm.github.check_run.completed",
        "scm.github.workflow_run.completed",
        "repo.cloned",
        "repo.created",
        "repo.worktree_created",
        "schedule.fire",
        "manual.run",
    }
)


class AutomationContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _contract_error(code: str, message: str) -> AutomationContractError:
    return AutomationContractError(code, message)


def normalize_timestamp(value: Any, *, fallback: Optional[str] = None) -> str:
    text = _normalize_text(value)
    if text is None:
        return fallback or now_iso()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return dict(value)


def normalize_optional_json_object(
    value: Any, *, field_name: str
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object when present")
    return dict(value)


def normalize_bool(value: Any, *, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def normalize_non_negative_int(value: Any, *, fallback: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def normalize_positive_int(value: Any, *, fallback: int = 1) -> int:
    parsed = normalize_non_negative_int(value, fallback=fallback)
    return parsed if parsed > 0 else fallback


def require_choice(value: Any, *, field_name: str, choices: frozenset[str]) -> str:
    text = _normalize_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    normalized = text.lower()
    if normalized not in choices:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(choices))}")
    return normalized


def optional_text(value: Any) -> Optional[str]:
    return _normalize_text(value)


def _require_optional_string_list(
    value: Any, *, field_name: str, allow_templates: bool = True
) -> list[str]:
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_LIST",
            f"{field_name} must be a non-empty string list",
        )
    normalized: list[str] = []
    for item in raw_values:
        text = optional_text(item)
        if text is None:
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_LIST",
                f"{field_name} must contain only non-empty strings",
            )
        if not allow_templates and "{{" in text:
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_TEMPLATE",
                f"{field_name} does not allow templates",
            )
        normalized.append(text)
    if not normalized:
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_LIST",
            f"{field_name} must be a non-empty string list",
        )
    return normalized


def _require_int_range(value: Any, *, field_name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_NUMBER",
            f"{field_name} must be an integer from {low} through {high}",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_NUMBER",
            f"{field_name} must be an integer from {low} through {high}",
        ) from exc
    if parsed < low or parsed > high:
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_NUMBER",
            f"{field_name} must be an integer from {low} through {high}",
        )
    return parsed


def validate_trigger_contract(
    trigger_kind: str, trigger: dict[str, Any]
) -> dict[str, Any]:
    normalized = dict(trigger)
    if "event_type" in normalized:
        raise _contract_error(
            "AUTOMATION_CONTRACT_LEGACY_TRIGGER",
            "trigger.event_type is legacy; use trigger.event_types instead",
        )
    if trigger_kind == TRIGGER_KIND_EVENT:
        event_types = _require_optional_string_list(
            normalized.get("event_types"), field_name="trigger.event_types"
        )
        normalized["event_types"] = event_types
    elif trigger_kind == TRIGGER_KIND_SCHEDULE:
        schedule_kind = normalized.get("schedule_kind")
        if schedule_kind is not None:
            normalized["schedule_kind"] = require_choice(
                schedule_kind,
                field_name="trigger.schedule_kind",
                choices=SCHEDULE_KINDS,
            )
        else:
            event_types = _require_optional_string_list(
                normalized.get("event_types"), field_name="trigger.event_types"
            )
            if "schedule.fire" not in event_types:
                raise _contract_error(
                    "AUTOMATION_CONTRACT_INVALID_TRIGGER",
                    "schedule trigger event_types must include schedule.fire",
                )
            normalized["event_types"] = event_types
    elif trigger_kind == TRIGGER_KIND_MANUAL:
        if "event_types" in normalized:
            event_types = _require_optional_string_list(
                normalized.get("event_types"), field_name="trigger.event_types"
            )
            if "manual.run" not in event_types:
                raise _contract_error(
                    "AUTOMATION_CONTRACT_INVALID_TRIGGER",
                    "manual trigger event_types must include manual.run",
                )
            normalized["event_types"] = event_types
    return normalized


def validate_target_contract(
    target_policy: str, target: dict[str, Any]
) -> dict[str, Any]:
    normalized = dict(target)
    if "policy" in normalized and normalized.get("policy") != target_policy:
        raise _contract_error(
            "AUTOMATION_CONTRACT_TARGET_POLICY_MISMATCH",
            "target.policy must match target_policy when present",
        )
    for key in ("repo_id", "base_repo_id", "worktree_id", "thread_target_id"):
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_TARGET",
                f"target.{key} must be a string when present",
            )
    if target_policy == TARGET_POLICY_NEW_AUTOMATION_WORKTREE and not normalized.get(
        "base_repo_id"
    ):
        raise _contract_error(
            "AUTOMATION_CONTRACT_TARGET_REQUIRED",
            "new_automation_worktree requires target.base_repo_id",
        )
    return normalized


def validate_executor_contract(
    executor_kind: str, executor: dict[str, Any]
) -> dict[str, Any]:
    normalized = dict(executor)
    kind = normalized.get("kind")
    if kind is not None and kind != executor_kind:
        raise _contract_error(
            "AUTOMATION_CONTRACT_EXECUTOR_KIND_MISMATCH",
            "executor.kind must match executor_kind when present",
        )
    if executor_kind in LEGACY_EXECUTOR_KINDS:
        normalized["legacy_executor_kind"] = executor_kind
    for key in ("message", "prompt", "prompt_template", "operation_kind"):
        value = normalized.get(key)
        if value is not None and not isinstance(value, str):
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_EXECUTOR",
                f"executor.{key} must be a string when present",
            )
    if executor_kind in {EXECUTOR_AGENT_TASK_TURN, EXECUTOR_PMA_OPERATOR_TURN}:
        requested_runtime = normalized.get("requested_runtime")
        if requested_runtime is not None:
            normalized["requested_runtime"] = AutomationRuntimeContract.from_dict(
                requested_runtime, field_name="executor.requested_runtime"
            ).to_dict()
    if executor_kind == EXECUTOR_TICKET_FLOW:
        ticket_pack = normalized.get("ticket_pack") or normalized.get("pack")
        if ticket_pack is not None and not isinstance(ticket_pack, dict):
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_TICKET_PACK",
                "ticket_flow executor ticket_pack must be an object",
            )
    if executor_kind == EXECUTOR_PUBLISH_OPERATION:
        payload = normalized.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_PUBLISH_OPERATION",
                "publish_operation executor payload must be an object",
            )
    if executor_kind == EXECUTOR_PUBLISH_CHAT_NOTIFICATION:
        delivery = normalized.get("delivery")
        if delivery is not None and not isinstance(delivery, str):
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_PUBLISH_OPERATION",
                "publish_chat_notification executor delivery must be a string",
            )
    actions = normalized.get("actions")
    if actions is not None and not isinstance(actions, (dict, list)):
        raise _contract_error(
            "AUTOMATION_CONTRACT_INVALID_ACTIONS",
            "executor.actions must be an object or list when present",
        )
    return normalized


def validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(policy)
    normalized["max_attempts"] = normalize_positive_int(
        normalized.get("max_attempts"), fallback=3
    )
    normalized["max_concurrent_per_rule"] = normalize_positive_int(
        normalized.get("max_concurrent_per_rule"), fallback=1
    )
    normalized["max_concurrent_per_target"] = normalize_positive_int(
        normalized.get("max_concurrent_per_target"), fallback=1
    )
    approval = normalized.get("approval_mode", APPROVAL_PAUSE_AND_REQUEST_USER)
    normalized["approval_mode"] = require_choice(
        approval, field_name="approval_mode", choices=APPROVAL_MODES
    )
    return normalized


def validate_schedule_contract(
    schedule_kind: str,
    schedule: dict[str, Any],
    *,
    next_fire_at: Optional[str],
    state: str = "active",
) -> dict[str, Any]:
    normalized = dict(schedule)
    if "due_at" in normalized:
        raise _contract_error(
            "AUTOMATION_CONTRACT_LEGACY_SCHEDULE",
            "schedule.due_at is legacy; use next_fire_at for one-shot schedules",
        )
    if schedule_kind == SCHEDULE_ONE_SHOT:
        if next_fire_at is None and state == "active":
            raise _contract_error(
                "AUTOMATION_CONTRACT_SCHEDULE_TIME_REQUIRED",
                "active one_shot schedules require next_fire_at",
            )
    elif schedule_kind == SCHEDULE_INTERVAL:
        interval_seconds = _require_int_range(
            normalized.get("interval_seconds"),
            field_name="schedule.interval_seconds",
            low=1,
            high=31_536_000,
        )
        normalized["interval_seconds"] = interval_seconds
    elif schedule_kind == SCHEDULE_DAILY:
        if "hour" in normalized or "minute" in normalized:
            normalized["hour"] = _require_int_range(
                normalized.get("hour"), field_name="schedule.hour", low=0, high=23
            )
            normalized["minute"] = _require_int_range(
                normalized.get("minute"),
                field_name="schedule.minute",
                low=0,
                high=59,
            )
    elif schedule_kind == SCHEDULE_WEEKLY:
        weekdays = normalized.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            raise _contract_error(
                "AUTOMATION_CONTRACT_INVALID_WEEKDAYS",
                "weekly schedules require schedule.weekdays as a non-empty list",
            )
        normalized["weekdays"] = [
            _require_int_range(item, field_name="schedule.weekdays[]", low=0, high=6)
            for item in weekdays
        ]
        normalized["hour"] = _require_int_range(
            normalized.get("hour"), field_name="schedule.hour", low=0, high=23
        )
        normalized["minute"] = _require_int_range(
            normalized.get("minute"), field_name="schedule.minute", low=0, high=59
        )
    return normalized


def validate_job_transition(from_state: str, to_state: str) -> None:
    current = require_choice(from_state, field_name="from_state", choices=JOB_STATES)
    target = require_choice(to_state, field_name="to_state", choices=JOB_STATES)
    if current == target:
        return
    if current in JOB_TERMINAL_STATES:
        raise ValueError(f"job state {current} is terminal")
    allowed = JOB_STATE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"invalid job state transition: {current} -> {target}")


def default_dedupe_key(*, rule_id: str, event_id: str, target: dict[str, Any]) -> str:
    import json

    source = json.dumps(
        {"rule_id": rule_id, "event_id": event_id, "target": target},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AutomationRuntimeContract:
    agent: Optional[str] = None
    model: Optional[str] = None
    profile: Optional[str] = None
    reasoning: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[str] = None
    prompt_ref: Optional[dict[str, Any]] = None
    input_ref: Optional[dict[str, Any]] = None
    workspace_scope: Optional[dict[str, Any]] = None
    backend_runtime_id: Optional[str] = None
    provider_payload: Optional[dict[str, Any]] = None

    @classmethod
    def from_dict(
        cls, value: Any, *, field_name: str = "runtime"
    ) -> "AutomationRuntimeContract":
        data = normalize_json_object(value, field_name=field_name)
        return cls(
            agent=optional_text(data.get("agent")),
            model=optional_text(data.get("model")),
            profile=optional_text(data.get("profile")),
            reasoning=optional_text(data.get("reasoning")),
            approval_policy=optional_text(data.get("approval_policy")),
            sandbox_policy=optional_text(data.get("sandbox_policy")),
            prompt_ref=normalize_optional_json_object(
                data.get("prompt_ref"), field_name=f"{field_name}.prompt_ref"
            ),
            input_ref=normalize_optional_json_object(
                data.get("input_ref"), field_name=f"{field_name}.input_ref"
            ),
            workspace_scope=normalize_optional_json_object(
                data.get("workspace_scope"),
                field_name=f"{field_name}.workspace_scope",
            ),
            backend_runtime_id=optional_text(data.get("backend_runtime_id")),
            provider_payload=normalize_optional_json_object(
                data.get("provider_payload"),
                field_name=f"{field_name}.provider_payload",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutomationChildExecutionEdge:
    edge_id: str
    parent_job_id: str
    child_kind: str
    child_id: str
    authoritative_for_parent_completion: bool
    requested_runtime: AutomationRuntimeContract
    actual_runtime: Optional[AutomationRuntimeContract]
    terminal_mapping: dict[str, str]
    terminal_event_id: Optional[str]
    terminal_state: Optional[str]
    terminal_observed_at: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        parent_job_id: str,
        child_kind: str,
        child_id: str,
        requested_runtime: AutomationRuntimeContract | dict[str, Any],
        actual_runtime: Optional[AutomationRuntimeContract | dict[str, Any]] = None,
        authoritative_for_parent_completion: bool = True,
        terminal_mapping: Optional[dict[str, Any]] = None,
        terminal_event_id: Optional[str] = None,
        terminal_state: Optional[str] = None,
        terminal_observed_at: Optional[str] = None,
        edge_id: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "AutomationChildExecutionEdge":
        resolved_parent_job_id = optional_text(parent_job_id)
        resolved_child_id = optional_text(child_id)
        if resolved_parent_job_id is None:
            raise ValueError("parent_job_id is required")
        if resolved_child_id is None:
            raise ValueError("child_id is required")
        normalized_child_kind = require_choice(
            child_kind, field_name="child_kind", choices=AUTOMATION_CHILD_KINDS
        )
        requested = (
            requested_runtime
            if isinstance(requested_runtime, AutomationRuntimeContract)
            else AutomationRuntimeContract.from_dict(
                requested_runtime, field_name="requested_runtime"
            )
        )
        actual = (
            actual_runtime
            if isinstance(actual_runtime, AutomationRuntimeContract)
            else (
                AutomationRuntimeContract.from_dict(
                    actual_runtime, field_name="actual_runtime"
                )
                if actual_runtime is not None
                else None
            )
        )
        mapping = normalize_json_object(
            terminal_mapping or AUTOMATION_CHILD_TERMINAL_MAPPING,
            field_name="terminal_mapping",
        )
        normalized_mapping: dict[str, str] = {}
        for key, value in mapping.items():
            normalized_key = optional_text(key)
            normalized_value = optional_text(value)
            if normalized_key is None or normalized_value is None:
                raise ValueError("terminal_mapping must map strings to strings")
            normalized_mapping[normalized_key] = require_choice(
                normalized_value,
                field_name="terminal_mapping value",
                choices=JOB_STATES,
            )
        stamp = normalize_timestamp(created_at)
        return cls(
            edge_id=optional_text(edge_id) or str(uuid.uuid4()),
            parent_job_id=resolved_parent_job_id,
            child_kind=normalized_child_kind,
            child_id=resolved_child_id,
            authoritative_for_parent_completion=normalize_bool(
                authoritative_for_parent_completion, fallback=True
            ),
            requested_runtime=requested,
            actual_runtime=actual,
            terminal_mapping=normalized_mapping,
            terminal_event_id=optional_text(terminal_event_id),
            terminal_state=optional_text(terminal_state),
            terminal_observed_at=(
                normalize_timestamp(terminal_observed_at)
                if terminal_observed_at is not None
                else None
            ),
            created_at=stamp,
            updated_at=normalize_timestamp(updated_at, fallback=stamp),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "AutomationChildExecutionEdge":
        data = normalize_json_object(value, field_name="child_execution_edge")
        parent_job_id = optional_text(data.get("parent_job_id"))
        child_kind = optional_text(data.get("child_kind"))
        child_id = optional_text(data.get("child_id"))
        requested_runtime = data.get("requested_runtime")
        if parent_job_id is None:
            raise ValueError("parent_job_id is required")
        if child_kind is None:
            raise ValueError("child_kind is required")
        if child_id is None:
            raise ValueError("child_id is required")
        if not isinstance(requested_runtime, dict):
            raise ValueError("requested_runtime is required")
        return cls.create(
            edge_id=data.get("edge_id"),
            parent_job_id=parent_job_id,
            child_kind=child_kind,
            child_id=child_id,
            authoritative_for_parent_completion=data.get(
                "authoritative_for_parent_completion", True
            ),
            requested_runtime=requested_runtime,
            actual_runtime=data.get("actual_runtime"),
            terminal_mapping=data.get("terminal_mapping"),
            terminal_event_id=data.get("terminal_event_id"),
            terminal_state=data.get("terminal_state"),
            terminal_observed_at=data.get("terminal_observed_at"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requested_runtime"] = self.requested_runtime.to_dict()
        data["actual_runtime"] = (
            self.actual_runtime.to_dict() if self.actual_runtime is not None else None
        )
        return data


@dataclass
class AutomationRule:
    rule_id: str
    name: str
    enabled: bool
    system_owned: bool
    trigger_kind: str
    trigger: dict[str, Any]
    filters: dict[str, Any]
    target_policy: str
    target: dict[str, Any]
    executor_kind: str
    executor: dict[str, Any]
    policy: dict[str, Any]
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        trigger_kind: str,
        trigger: Optional[dict[str, Any]] = None,
        filters: Optional[dict[str, Any]] = None,
        target_policy: str = TARGET_POLICY_HUB,
        target: Optional[dict[str, Any]] = None,
        executor_kind: str,
        executor: Optional[dict[str, Any]] = None,
        policy: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        rule_id: Optional[str] = None,
        enabled: bool = True,
        system_owned: bool = False,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "AutomationRule":
        normalized_trigger_kind = require_choice(
            trigger_kind, field_name="trigger_kind", choices=TRIGGER_KINDS
        )
        normalized_target_policy = require_choice(
            target_policy, field_name="target_policy", choices=TARGET_POLICIES
        )
        normalized_executor_kind = require_choice(
            executor_kind, field_name="executor_kind", choices=EXECUTOR_INPUT_KINDS
        )
        normalized_trigger = validate_trigger_contract(
            normalized_trigger_kind,
            normalize_json_object(trigger, field_name="trigger"),
        )
        normalized_target = validate_target_contract(
            normalized_target_policy,
            normalize_json_object(target, field_name="target"),
        )
        normalized_executor = validate_executor_contract(
            normalized_executor_kind,
            normalize_json_object(executor, field_name="executor"),
        )
        stamp = normalize_timestamp(created_at)
        return cls(
            rule_id=optional_text(rule_id) or str(uuid.uuid4()),
            name=optional_text(name) or "Untitled automation",
            enabled=normalize_bool(enabled, fallback=True),
            system_owned=normalize_bool(system_owned, fallback=False),
            trigger_kind=normalized_trigger_kind,
            trigger=normalized_trigger,
            filters=normalize_json_object(filters, field_name="filters"),
            target_policy=normalized_target_policy,
            target=normalized_target,
            executor_kind=normalized_executor_kind,
            executor=normalized_executor,
            policy=validate_policy(normalize_json_object(policy, field_name="policy")),
            metadata=normalize_json_object(metadata, field_name="metadata"),
            created_at=stamp,
            updated_at=normalize_timestamp(updated_at, fallback=stamp),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutomationEvent:
    event_id: str
    event_type: str
    observed_at: str
    source: Optional[str] = None
    repo_id: Optional[str] = None
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        observed_at: Optional[str] = None,
        source: Optional[str] = None,
        repo_id: Optional[str] = None,
        target: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        raw_payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        event_id: Optional[str] = None,
    ) -> "AutomationEvent":
        normalized_event_type = require_choice(
            event_type, field_name="event_type", choices=AUTOMATION_EVENT_TYPES
        )
        return cls(
            event_id=optional_text(event_id) or str(uuid.uuid4()),
            event_type=normalized_event_type,
            observed_at=normalize_timestamp(observed_at),
            source=optional_text(source),
            repo_id=optional_text(repo_id),
            target=normalize_json_object(target, field_name="target"),
            payload=normalize_json_object(payload, field_name="payload"),
            raw_payload=normalize_json_object(raw_payload, field_name="raw_payload"),
            metadata=normalize_json_object(metadata, field_name="metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutomationJob:
    job_id: str
    rule_id: str
    event_id: str
    state: str
    dedupe_key: str
    batch_key: Optional[str]
    lock_key: Optional[str]
    available_at: str
    claimed_at: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    created_at: str
    updated_at: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: Optional[str]
    retry_backoff_seconds: int
    target: dict[str, Any]
    executor: dict[str, Any]
    policy: dict[str, Any]
    payload: dict[str, Any]
    managed_thread_target_id: Optional[str] = None
    managed_thread_execution_id: Optional[str] = None
    pma_lane_id: Optional[str] = None
    pma_queue_item_id: Optional[str] = None
    ticket_flow_repo_id: Optional[str] = None
    ticket_flow_run_id: Optional[str] = None
    ticket_flow_worktree_id: Optional[str] = None
    publish_operation_id: Optional[str] = None
    result_summary: Optional[str] = None
    error_text: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        rule_id: str,
        event_id: str,
        target: dict[str, Any],
        executor: dict[str, Any],
        policy: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
        batch_key: Optional[str] = None,
        lock_key: Optional[str] = None,
        available_at: Optional[str] = None,
        max_attempts: Optional[int] = None,
        job_id: Optional[str] = None,
        state: str = JOB_PENDING,
        created_at: Optional[str] = None,
    ) -> "AutomationJob":
        normalized_policy = validate_policy(
            normalize_json_object(policy, field_name="policy")
        )
        resolved_target = normalize_json_object(target, field_name="target")
        resolved_event_id = optional_text(event_id)
        resolved_rule_id = optional_text(rule_id)
        if resolved_rule_id is None:
            raise ValueError("rule_id is required")
        if resolved_event_id is None:
            raise ValueError("event_id is required")
        resolved_dedupe_key = optional_text(dedupe_key) or default_dedupe_key(
            rule_id=resolved_rule_id,
            event_id=resolved_event_id,
            target=resolved_target,
        )
        stamp = now_iso()
        resolved_created_at = normalize_timestamp(created_at, fallback=stamp)
        return cls(
            job_id=optional_text(job_id) or str(uuid.uuid4()),
            rule_id=resolved_rule_id,
            event_id=resolved_event_id,
            state=require_choice(state, field_name="state", choices=JOB_STATES),
            dedupe_key=resolved_dedupe_key,
            batch_key=optional_text(batch_key),
            lock_key=optional_text(lock_key),
            available_at=normalize_timestamp(available_at),
            claimed_at=None,
            started_at=None,
            finished_at=None,
            created_at=resolved_created_at,
            updated_at=stamp,
            attempt_count=0,
            max_attempts=normalize_positive_int(
                max_attempts or normalized_policy.get("max_attempts"), fallback=3
            ),
            next_attempt_at=None,
            retry_backoff_seconds=normalize_non_negative_int(
                normalized_policy.get("retry_backoff_seconds"), fallback=0
            ),
            target=resolved_target,
            executor=normalize_json_object(executor, field_name="executor"),
            policy=normalized_policy,
            payload=normalize_json_object(payload, field_name="payload"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutomationJobAttempt:
    attempt_id: str
    job_id: str
    attempt_number: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    error_text: Optional[str] = None
    executor_result: dict[str, Any] = field(default_factory=dict)
    execution_refs: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        attempt_number: int,
        status: str = JOB_RUNNING,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        error_text: Optional[str] = None,
        executor_result: Optional[dict[str, Any]] = None,
        execution_refs: Optional[dict[str, Any]] = None,
        attempt_id: Optional[str] = None,
    ) -> "AutomationJobAttempt":
        resolved_job_id = optional_text(job_id)
        if resolved_job_id is None:
            raise ValueError("job_id is required")
        return cls(
            attempt_id=optional_text(attempt_id) or str(uuid.uuid4()),
            job_id=resolved_job_id,
            attempt_number=normalize_positive_int(attempt_number, fallback=1),
            status=require_choice(status, field_name="status", choices=JOB_STATES),
            started_at=normalize_timestamp(started_at),
            finished_at=(
                normalize_timestamp(finished_at) if optional_text(finished_at) else None
            ),
            error_text=optional_text(error_text),
            executor_result=normalize_json_object(
                executor_result, field_name="executor_result"
            ),
            execution_refs=normalize_json_object(
                execution_refs, field_name="execution_refs"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AutomationSchedule:
    schedule_id: str
    rule_id: str
    schedule_kind: str
    timezone: str
    next_fire_at: Optional[str]
    last_fire_at: Optional[str]
    misfire_policy: str
    schedule: dict[str, Any]
    state: str
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        rule_id: str,
        schedule_kind: str,
        timezone: str = "UTC",
        next_fire_at: Optional[str] = None,
        last_fire_at: Optional[str] = None,
        misfire_policy: str = "fire_once",
        schedule: Optional[dict[str, Any]] = None,
        state: str = "active",
        schedule_id: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> "AutomationSchedule":
        resolved_rule_id = optional_text(rule_id)
        if resolved_rule_id is None:
            raise ValueError("rule_id is required")
        normalized_schedule_kind = require_choice(
            schedule_kind, field_name="schedule_kind", choices=SCHEDULE_KINDS
        )
        normalized_next_fire_at = (
            normalize_timestamp(next_fire_at) if optional_text(next_fire_at) else None
        )
        normalized_schedule = validate_schedule_contract(
            normalized_schedule_kind,
            normalize_json_object(schedule, field_name="schedule"),
            next_fire_at=normalized_next_fire_at,
            state=optional_text(state) or "active",
        )
        stamp = normalize_timestamp(created_at)
        return cls(
            schedule_id=optional_text(schedule_id) or str(uuid.uuid4()),
            rule_id=resolved_rule_id,
            schedule_kind=normalized_schedule_kind,
            timezone=optional_text(timezone) or "UTC",
            next_fire_at=normalized_next_fire_at,
            last_fire_at=(
                normalize_timestamp(last_fire_at)
                if optional_text(last_fire_at)
                else None
            ),
            misfire_policy=optional_text(misfire_policy) or "fire_once",
            schedule=normalized_schedule,
            state=optional_text(state) or "active",
            created_at=stamp,
            updated_at=normalize_timestamp(updated_at, fallback=stamp),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "APPROVAL_MODES",
    "AUTOMATION_EVENT_TYPES",
    "AUTOMATION_CHILD_KINDS",
    "AUTOMATION_CHILD_KIND_AGENT_TASK",
    "AUTOMATION_CHILD_KIND_PMA_OPERATOR",
    "AUTOMATION_CHILD_KIND_PUBLISH_OPERATION",
    "AUTOMATION_CHILD_KIND_TICKET_FLOW",
    "AUTOMATION_CHILD_TERMINAL_MAPPING",
    "AutomationChildExecutionEdge",
    "AutomationContractError",
    "AutomationEvent",
    "AutomationJob",
    "AutomationJobAttempt",
    "AutomationRule",
    "AutomationRuntimeContract",
    "AutomationSchedule",
    "EXECUTOR_AGENT_TASK_TURN",
    "EXECUTOR_INPUT_KINDS",
    "EXECUTOR_KINDS",
    "EXECUTOR_PMA_OPERATOR_TURN",
    "JOB_CLAIMED",
    "JOB_DEAD_LETTERED",
    "JOB_FAILED",
    "JOB_PENDING",
    "JOB_RUNNING",
    "JOB_STATE_TRANSITIONS",
    "JOB_STATES",
    "JOB_SUCCEEDED",
    "LEGACY_EXECUTOR_KINDS",
    "LEGACY_EXECUTOR_MANAGED_THREAD_TURN",
    "LEGACY_EXECUTOR_PMA_TURN",
    "SCHEDULE_KINDS",
    "TARGET_POLICIES",
    "TRIGGER_KINDS",
    "default_dedupe_key",
    "normalize_timestamp",
    "validate_job_transition",
    "validate_policy",
    "validate_schedule_contract",
    "validate_executor_contract",
    "validate_target_contract",
    "validate_trigger_contract",
]
