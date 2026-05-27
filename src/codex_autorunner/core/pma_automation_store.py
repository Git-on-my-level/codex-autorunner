from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .pma_automation_domain_translation import PmaAutomationDomainTranslator
from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
)
from .pma_automation_services import (
    MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES,
    PmaAutomationThreadNotFoundError,
    PmaSubscriptionCommandService,
    PmaWakeupDispatchDecisionService,
)
from .pma_automation_subscription_store import PmaAutomationSubscriptionStoreMixin
from .pma_automation_timer_store import PmaAutomationTimerStoreMixin
from .pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    PMA_AUTOMATION_STORE_FILENAME,
    PMA_AUTOMATION_VERSION,
    default_pma_automation_state,
)
from .pma_automation_wakeup_store import PmaAutomationWakeupStoreMixin
from .pma_domain.subscription_reducer import ReduceTransitionResult


class PmaAutomationStore(
    PmaAutomationSubscriptionStoreMixin,
    PmaAutomationTimerStoreMixin,
    PmaAutomationWakeupStoreMixin,
):
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = hub_root
        self._durable = durable
        self._persistence = PmaAutomationPersistence(hub_root, durable=durable)
        self._subscriptions = PmaSubscriptionCommandService(hub_root, self._persistence)
        self._dispatch_decisions = PmaWakeupDispatchDecisionService(hub_root)

    @property
    def path(self) -> Path:
        return self._persistence.path

    def _lock_path(self) -> Path:
        return self._persistence._lock_path()

    def load(self) -> dict[str, Any]:
        return self._persistence.load()

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        return self._persistence._load_unlocked()

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        return self._persistence._load_structured_unlocked()

    def _normalize_subscriptions(self, value: Any) -> list[PmaLifecycleSubscription]:
        return self._persistence._normalize_subscriptions(value)

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]:
        return self._persistence._normalize_timers(value)

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]:
        return self._persistence._normalize_wakeups(value)

    @staticmethod
    def _coerce_payload(
        payload: Optional[dict[str, Any]], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if isinstance(payload, dict):
            merged.update(payload)
        for key, value in kwargs.items():
            if value is not None:
                merged[key] = value
        return merged

    def _compute_dispatch_decision_for_wakeup(
        self, wakeup: PmaAutomationWakeup
    ) -> None:
        self._dispatch_decisions.enrich(wakeup)

    @staticmethod
    def _lifecycle_sub_to_domain(entry: PmaLifecycleSubscription) -> Any:
        return PmaAutomationDomainTranslator._lifecycle_sub_to_domain(entry)

    @staticmethod
    def _store_timer_to_domain(entry: PmaAutomationTimer) -> Any:
        return PmaAutomationDomainTranslator._store_timer_to_domain(entry)

    @staticmethod
    def _store_wakeup_to_domain(entry: PmaAutomationWakeup) -> Any:
        return PmaAutomationDomainTranslator._store_wakeup_to_domain(entry)

    @staticmethod
    def _apply_domain_timer_to_store(
        store_timer: PmaAutomationTimer,
        domain_timer: Any,
    ) -> None:
        PmaAutomationDomainTranslator._apply_domain_timer_to_store(
            store_timer,
            domain_timer,
        )

    @staticmethod
    def _apply_domain_wakeup_to_store(
        store_wakeup: PmaAutomationWakeup,
        domain_wakeup: Any,
    ) -> None:
        PmaAutomationDomainTranslator._apply_domain_wakeup_to_store(
            store_wakeup,
            domain_wakeup,
        )

    def _apply_reduce_result(
        self,
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: bool = True,
    ) -> list[PmaAutomationWakeup]:
        return PmaAutomationDomainTranslator._apply_reduce_result(
            subscriptions,
            wakeups,
            result,
            timestamp,
            compute_dispatch=(
                self._compute_dispatch_decision_for_wakeup if compute_dispatch else None
            ),
        )

    @staticmethod
    def _coerce_limit(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            parsed = int(value)
        except (ValueError, TypeError):
            return None
        if parsed < 0:
            return None
        return parsed


__all__ = [
    "DEFAULT_PMA_LANE_ID",
    "MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES",
    "PMA_AUTOMATION_STORE_FILENAME",
    "PMA_AUTOMATION_VERSION",
    "PmaAutomationStore",
    "PmaAutomationThreadNotFoundError",
    "PmaAutomationTimer",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "default_pma_automation_state",
]
