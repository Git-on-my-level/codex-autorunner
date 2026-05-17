from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .automation import AutomationStore
from .pma_automation_records import PmaAutomationTimer, PmaLifecycleSubscription
from .pma_automation_unified import (
    PmaUnifiedAutomationAdapter,
    PmaUnifiedMirrorResult,
)

logger = logging.getLogger(__name__)


class PmaAutomationMirror:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable

    def mirror_subscription_rule(
        self, subscription: PmaLifecycleSubscription
    ) -> PmaUnifiedMirrorResult:
        subscription_id = getattr(subscription, "subscription_id", None)
        try:
            rule = PmaUnifiedAutomationAdapter(
                AutomationStore(self._hub_root, durable=self._durable)
            ).mirror_subscription_rule(subscription=subscription)
            return PmaUnifiedMirrorResult(
                operation="mirror_subscription_rule",
                identifier=str(subscription_id or ""),
                ok=True,
                rule_id=rule.rule_id,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
            logger.exception(
                "Failed to mirror PMA subscription into unified automation rule: %s",
                subscription_id,
            )
            return PmaUnifiedMirrorResult(
                operation="mirror_subscription_rule",
                identifier=str(subscription_id or ""),
                ok=False,
                error=str(exc),
            )

    def mirror_timer_schedule(
        self, timer: PmaAutomationTimer
    ) -> PmaUnifiedMirrorResult:
        timer_id = getattr(timer, "timer_id", None)
        try:
            rule, schedule = PmaUnifiedAutomationAdapter(
                AutomationStore(self._hub_root, durable=self._durable)
            ).mirror_timer_schedule(timer=timer)
            return PmaUnifiedMirrorResult(
                operation="mirror_timer_schedule",
                identifier=str(timer_id or ""),
                ok=True,
                rule_id=rule.rule_id,
                schedule_id=schedule.schedule_id,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
            logger.exception(
                "Failed to mirror PMA timer into unified automation schedule: %s",
                timer_id,
            )
            return PmaUnifiedMirrorResult(
                operation="mirror_timer_schedule",
                identifier=str(timer_id or ""),
                ok=False,
                error=str(exc),
            )


__all__ = ["PmaAutomationMirror", "PmaUnifiedMirrorResult"]
