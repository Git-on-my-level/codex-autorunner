from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class FlowNotificationIntentRecord:
    intent_id: str
    run_id: str
    event_type: str
    severity: str
    reason: str
    recommended_actions: tuple[str, ...]
    cooldown_seconds: int
    resolved: bool
    first_seen_at: str
    last_observed_at: str
    last_notified_at: Optional[str]
    resolved_at: Optional[str]
    observed_count: int
    payload: Mapping[str, Any]
    delivery_attempts: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "reason": self.reason,
            "recommended_actions": list(self.recommended_actions),
            "cooldown_seconds": self.cooldown_seconds,
            "resolved": self.resolved,
            "first_seen_at": self.first_seen_at,
            "last_observed_at": self.last_observed_at,
            "last_notified_at": self.last_notified_at,
            "resolved_at": self.resolved_at,
            "observed_count": self.observed_count,
            "payload": dict(self.payload),
            "delivery_attempts": dict(self.delivery_attempts),
        }
