from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .automation import (
    AutomationStore,
    mirror_pma_subscription_rule,
    mirror_pma_timer_schedule,
)
from .pma_automation_records import PmaAutomationTimer, PmaLifecycleSubscription

logger = logging.getLogger(__name__)


class PmaAutomationMirror:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable

    def mirror_subscription_rule(self, subscription: PmaLifecycleSubscription) -> None:
        try:
            mirror_pma_subscription_rule(
                AutomationStore(self._hub_root, durable=self._durable),
                subscription=subscription,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
            logger.exception(
                "Failed to mirror PMA subscription into unified automation rule: %s",
                getattr(subscription, "subscription_id", None),
            )

    def mirror_timer_schedule(self, timer: PmaAutomationTimer) -> None:
        try:
            mirror_pma_timer_schedule(
                AutomationStore(self._hub_root, durable=self._durable),
                timer=timer,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError):
            logger.exception(
                "Failed to mirror PMA timer into unified automation schedule: %s",
                getattr(timer, "timer_id", None),
            )


__all__ = ["PmaAutomationMirror"]
