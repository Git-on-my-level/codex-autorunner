from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog

logger = logging.getLogger(__name__)


@dataclass
class PmaSafetyConfig:
    dedup_window_seconds: int = 300
    max_duplicate_actions: int = 3
    rate_limit_window_seconds: int = 60
    max_actions_per_window: int = 20
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_seconds: int = 600
    enable_dedup: bool = True
    enable_rate_limit: bool = True
    enable_circuit_breaker: bool = True


@dataclass
class SafetyCheckResult:
    allowed: bool
    reason: Optional[str] = None
    details: Optional[dict[str, Any]] = None

    def __post_init__(self):
        if self.details is None:
            object.__setattr__(self, "details", {})


class PmaSafetyChecker:
    def __init__(
        self, hub_root: Path, *, config: Optional[PmaSafetyConfig] = None
    ) -> None:
        self._hub_root = hub_root
        self._config = config or PmaSafetyConfig()
        self._audit_log = PmaAuditLog(hub_root)
        self._action_timestamps: defaultdict[str, list[float]] = defaultdict(list)
        self._failure_counts: defaultdict[str, int] = defaultdict(int)
        self._circuit_breaker_until: Optional[float] = None

    def _is_circuit_breaker_active(self) -> bool:
        if not self._config.enable_circuit_breaker:
            return False
        if self._circuit_breaker_until is None:
            return False
        now = datetime.now(timezone.utc).timestamp()
        if now >= self._circuit_breaker_until:
            self._reset_circuit_breaker()
            return False
        return True

    def _activate_circuit_breaker(self) -> None:
        self._circuit_breaker_until = (
            datetime.now(timezone.utc).timestamp()
            + self._config.circuit_breaker_cooldown_seconds
        )
        logger.warning(
            "PMA circuit breaker activated (cooldown: %d seconds)",
            self._config.circuit_breaker_cooldown_seconds,
        )

    def _reset_circuit_breaker(self) -> None:
        if self._circuit_breaker_until:
            self._circuit_breaker_until = None
            self._failure_counts.clear()
            logger.info("PMA circuit breaker reset")

    def check_chat_start(
        self,
        agent: str,
        message: str,
        client_turn_id: Optional[str] = None,
    ) -> SafetyCheckResult:
        if self._is_circuit_breaker_active():
            return SafetyCheckResult(
                allowed=False,
                reason="circuit_breaker_active",
                details={
                    "cooldown_remaining_seconds": (
                        int(
                            self._circuit_breaker_until
                            - datetime.now(timezone.utc).timestamp()
                        )
                        if self._circuit_breaker_until
                        else 0
                    )
                },
            )

        if self._config.enable_dedup:
            fingerprint = self._compute_chat_fingerprint(agent, message)
            recent_count = self._audit_log.count_fingerprint(
                fingerprint, within_seconds=self._config.dedup_window_seconds
            )
            if recent_count >= self._config.max_duplicate_actions:
                logger.warning(
                    "PMA duplicate action blocked (fingerprint: %s, count: %d)",
                    fingerprint,
                    recent_count,
                )
                return SafetyCheckResult(
                    allowed=False,
                    reason="duplicate_action",
                    details={
                        "fingerprint": fingerprint,
                        "count": recent_count,
                        "max_allowed": self._config.max_duplicate_actions,
                        "window_seconds": self._config.dedup_window_seconds,
                    },
                )

        if self._config.enable_rate_limit:
            now = datetime.now(timezone.utc).timestamp()
            key = f"chat:{agent}"
            self._action_timestamps[key] = [
                ts
                for ts in self._action_timestamps[key]
                if now - ts < self._config.rate_limit_window_seconds
            ]
            if len(self._action_timestamps[key]) >= self._config.max_actions_per_window:
                return SafetyCheckResult(
                    allowed=False,
                    reason="rate_limit_exceeded",
                    details={
                        "agent": agent,
                        "count": len(self._action_timestamps[key]),
                        "max_allowed": self._config.max_actions_per_window,
                        "window_seconds": self._config.rate_limit_window_seconds,
                    },
                )
            self._action_timestamps[key].append(now)

        return SafetyCheckResult(allowed=True)

    def record_chat_result(
        self,
        agent: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if (
            status in ("error", "failed", "interrupted")
            and self._config.enable_circuit_breaker
        ):
            key = f"chat:{agent}"
            self._failure_counts[key] += 1
            if self._failure_counts[key] >= self._config.circuit_breaker_threshold:
                self._activate_circuit_breaker()
        else:
            key = f"chat:{agent}"
            self._failure_counts[key] = 0

    def record_action(
        self,
        action_type: PmaActionType,
        agent: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        status: str = "ok",
        error: Optional[str] = None,
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        client_turn_id: Optional[str] = None,
    ) -> str:
        entry = PmaAuditEntry(
            action_type=action_type,
            agent=agent,
            thread_id=thread_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            details=details or {},
            status=status,
            error=error,
        )
        entry_id = self._audit_log.append(entry)
        return entry_id

    def _compute_chat_fingerprint(self, agent: str, message: str) -> str:
        from .pma_audit import PmaAuditEntry

        temp_entry = PmaAuditEntry(
            action_type=PmaActionType.CHAT_STARTED,
            agent=agent,
            details={"message_truncated": message[:200]},
        )
        return temp_entry.fingerprint

    def get_stats(self) -> dict[str, Any]:
        recent = self._audit_log.list_recent(limit=100)
        by_type: dict[str, int] = {}
        for entry in recent:
            atype = entry.action_type.value
            by_type[atype] = by_type.get(atype, 0) + 1
        return {
            "circuit_breaker_active": self._is_circuit_breaker_active(),
            "circuit_breaker_cooldown_remaining": (
                int(
                    self._circuit_breaker_until - datetime.now(timezone.utc).timestamp()
                )
                if self._circuit_breaker_until
                else 0
            ),
            "recent_actions_count": len(recent),
            "recent_actions_by_type": by_type,
            "failure_counts": dict(self._failure_counts),
        }


__all__ = [
    "PmaSafetyConfig",
    "SafetyCheckResult",
    "PmaSafetyChecker",
]
