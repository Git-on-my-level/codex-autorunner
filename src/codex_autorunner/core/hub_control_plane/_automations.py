from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ._normalizers import (
    coerce_int,
    copy_mapping,
    normalize_bool,
    normalize_optional_text,
    normalize_required_text,
)

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_REDACTED = "[redacted]"


def redact_automation_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = redact_automation_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_automation_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_automation_payload(item) for item in value]
    return value


def redact_automation_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(redact_automation_payload(value))


@dataclass(frozen=True)
class AutomationRuleUpsertRequest:
    rule: dict[str, Any] = field(default_factory=dict)
    schedule: Optional[dict[str, Any]] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRuleUpsertRequest":
        return cls(
            rule=copy_mapping(data.get("rule") if "rule" in data else data),
            schedule=(
                copy_mapping(data.get("schedule"))
                if isinstance(data.get("schedule"), Mapping)
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rule": dict(self.rule), "schedule": self.schedule}


@dataclass(frozen=True)
class AutomationRuleLookupRequest:
    rule_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRuleLookupRequest":
        return cls(
            rule_id=normalize_required_text(data.get("rule_id"), field_name="rule_id")
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id}


@dataclass(frozen=True)
class AutomationRuleListRequest:
    enabled: Optional[bool] = None
    trigger_kind: Optional[str] = None
    limit: int = 100

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRuleListRequest":
        enabled = None
        if "enabled" in data and data.get("enabled") is not None:
            enabled = normalize_bool(data.get("enabled"), fallback=False)
        return cls(
            enabled=enabled,
            trigger_kind=normalize_optional_text(data.get("trigger_kind")),
            limit=max(0, coerce_int(data.get("limit", 100), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "trigger_kind": self.trigger_kind,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class AutomationRuleEnabledRequest:
    rule_id: str
    enabled: bool

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRuleEnabledRequest":
        return cls(
            rule_id=normalize_required_text(data.get("rule_id"), field_name="rule_id"),
            enabled=normalize_bool(data.get("enabled"), fallback=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "enabled": self.enabled}


@dataclass(frozen=True)
class AutomationRuleRunRequest:
    rule_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    dedupe_key: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationRuleRunRequest":
        return cls(
            rule_id=normalize_required_text(data.get("rule_id"), field_name="rule_id"),
            payload=copy_mapping(data.get("payload")),
            target=copy_mapping(data.get("target")),
            metadata=copy_mapping(data.get("metadata")),
            dedupe_key=normalize_optional_text(data.get("dedupe_key")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "payload": dict(self.payload),
            "target": dict(self.target),
            "metadata": dict(self.metadata),
            "dedupe_key": self.dedupe_key,
        }


@dataclass(frozen=True)
class AutomationJobListRequest:
    state: Optional[str] = None
    rule_id: Optional[str] = None
    limit: int = 100

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationJobListRequest":
        return cls(
            state=normalize_optional_text(data.get("state")),
            rule_id=normalize_optional_text(data.get("rule_id")),
            limit=max(0, coerce_int(data.get("limit", 100), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "rule_id": self.rule_id, "limit": self.limit}


@dataclass(frozen=True)
class AutomationJobLookupRequest:
    job_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationJobLookupRequest":
        return cls(
            job_id=normalize_required_text(data.get("job_id"), field_name="job_id")
        )

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id}


@dataclass(frozen=True)
class AutomationJobActionRequest:
    job_id: str
    available_at: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationJobActionRequest":
        return cls(
            job_id=normalize_required_text(data.get("job_id"), field_name="job_id"),
            available_at=normalize_optional_text(data.get("available_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "available_at": self.available_at}


@dataclass(frozen=True)
class AutomationEventListRequest:
    event_type: Optional[str] = None
    limit: int = 100

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationEventListRequest":
        return cls(
            event_type=normalize_optional_text(data.get("event_type")),
            limit=max(0, coerce_int(data.get("limit", 100), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "limit": self.limit}


@dataclass(frozen=True)
class AutomationEventLookupRequest:
    event_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationEventLookupRequest":
        return cls(
            event_id=normalize_required_text(
                data.get("event_id"), field_name="event_id"
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id}


@dataclass(frozen=True)
class AutomationScheduleListRequest:
    rule_id: Optional[str] = None
    limit: int = 100

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationScheduleListRequest":
        return cls(
            rule_id=normalize_optional_text(data.get("rule_id")),
            limit=max(0, coerce_int(data.get("limit", 100), field_name="limit")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "limit": self.limit}


@dataclass(frozen=True)
class AutomationScheduleLookupRequest:
    schedule_id: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutomationScheduleLookupRequest":
        return cls(
            schedule_id=normalize_required_text(
                data.get("schedule_id"), field_name="schedule_id"
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schedule_id": self.schedule_id}


@dataclass(frozen=True)
class AutomationRuleResponse:
    rule: Optional[dict[str, Any]]
    schedule: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "schedule": self.schedule}


@dataclass(frozen=True)
class AutomationRuleListResponse:
    rules: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [dict(rule) for rule in self.rules]}


@dataclass(frozen=True)
class AutomationJobResponse:
    job: Optional[dict[str, Any]]
    attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "attempts": [dict(attempt) for attempt in self.attempts],
        }


@dataclass(frozen=True)
class AutomationJobListResponse:
    jobs: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"jobs": [dict(job) for job in self.jobs]}


@dataclass(frozen=True)
class AutomationEventResponse:
    event: Optional[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event}


@dataclass(frozen=True)
class AutomationEventListResponse:
    events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"events": [dict(event) for event in self.events]}


@dataclass(frozen=True)
class AutomationScheduleResponse:
    schedule: Optional[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"schedule": self.schedule}


@dataclass(frozen=True)
class AutomationScheduleListResponse:
    schedules: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"schedules": [dict(schedule) for schedule in self.schedules]}


__all__ = [
    "AutomationEventListRequest",
    "AutomationEventListResponse",
    "AutomationEventLookupRequest",
    "AutomationEventResponse",
    "AutomationJobActionRequest",
    "AutomationJobListRequest",
    "AutomationJobListResponse",
    "AutomationJobLookupRequest",
    "AutomationJobResponse",
    "AutomationRuleEnabledRequest",
    "AutomationRuleListRequest",
    "AutomationRuleListResponse",
    "AutomationRuleLookupRequest",
    "AutomationRuleResponse",
    "AutomationRuleRunRequest",
    "AutomationRuleUpsertRequest",
    "AutomationScheduleListRequest",
    "AutomationScheduleListResponse",
    "AutomationScheduleLookupRequest",
    "AutomationScheduleResponse",
    "redact_automation_mapping",
    "redact_automation_payload",
]
