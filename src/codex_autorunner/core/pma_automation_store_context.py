from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol

from .pma_automation_persistence import PmaAutomationPersistence
from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
)
from .pma_automation_services import (
    PmaSubscriptionCommandService,
)
from .pma_domain.models import PmaSubscription, PmaTimer, PmaWakeup
from .pma_domain.subscription_reducer import ReduceTransitionResult


class PmaAutomationStoreContext(Protocol):
    _persistence: PmaAutomationPersistence
    _subscriptions: PmaSubscriptionCommandService

    def _lock_path(self) -> Path: ...

    def load(self) -> dict[str, Any]: ...

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]: ...

    def _normalize_subscriptions(
        self, value: Any
    ) -> list[PmaLifecycleSubscription]: ...

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]: ...

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]: ...

    def _coerce_payload(
        self,
        payload: Optional[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]: ...

    def _compute_dispatch_decision_for_wakeup(
        self,
        wakeup: PmaAutomationWakeup,
    ) -> None: ...

    def _coerce_limit(self, value: Any) -> Optional[int]: ...

    @staticmethod
    def _lifecycle_sub_to_domain(
        entry: PmaLifecycleSubscription,
    ) -> PmaSubscription: ...

    @staticmethod
    def _store_timer_to_domain(entry: PmaAutomationTimer) -> PmaTimer: ...

    @staticmethod
    def _store_wakeup_to_domain(
        entry: PmaAutomationWakeup,
    ) -> PmaWakeup: ...

    @staticmethod
    def _apply_domain_timer_to_store(
        entry: PmaAutomationTimer,
        timer: PmaTimer,
    ) -> None: ...

    @staticmethod
    def _apply_domain_wakeup_to_store(
        entry: PmaAutomationWakeup,
        wakeup: PmaWakeup,
    ) -> None: ...

    def _apply_reduce_result(
        self,
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: bool = True,
    ) -> list[PmaAutomationWakeup]: ...


class PmaAutomationStoreContextMixin:
    _persistence: PmaAutomationPersistence
    _subscriptions: PmaSubscriptionCommandService

    def _lock_path(self) -> Path:
        raise NotImplementedError

    def load(self) -> dict[str, Any]:
        raise NotImplementedError

    def _load_structured_unlocked(
        self,
    ) -> tuple[
        dict[str, Any],
        list[PmaLifecycleSubscription],
        list[PmaAutomationTimer],
        list[PmaAutomationWakeup],
    ]:
        raise NotImplementedError

    def _normalize_subscriptions(self, value: Any) -> list[PmaLifecycleSubscription]:
        raise NotImplementedError

    def _normalize_timers(self, value: Any) -> list[PmaAutomationTimer]:
        raise NotImplementedError

    def _normalize_wakeups(self, value: Any) -> list[PmaAutomationWakeup]:
        raise NotImplementedError

    def _coerce_payload(
        self,
        payload: Optional[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _compute_dispatch_decision_for_wakeup(
        self,
        wakeup: PmaAutomationWakeup,
    ) -> None:
        raise NotImplementedError

    def _coerce_limit(self, value: Any) -> Optional[int]:
        raise NotImplementedError

    @staticmethod
    def _lifecycle_sub_to_domain(entry: PmaLifecycleSubscription) -> PmaSubscription:
        raise NotImplementedError

    @staticmethod
    def _store_timer_to_domain(entry: PmaAutomationTimer) -> PmaTimer:
        raise NotImplementedError

    @staticmethod
    def _store_wakeup_to_domain(
        entry: PmaAutomationWakeup,
    ) -> PmaWakeup:
        raise NotImplementedError

    @staticmethod
    def _apply_domain_timer_to_store(
        entry: PmaAutomationTimer,
        timer: PmaTimer,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def _apply_domain_wakeup_to_store(
        entry: PmaAutomationWakeup,
        wakeup: PmaWakeup,
    ) -> None:
        raise NotImplementedError

    def _apply_reduce_result(
        self,
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: bool = True,
    ) -> list[PmaAutomationWakeup]:
        raise NotImplementedError


__all__ = [
    "PmaAutomationStoreContext",
    "PmaAutomationStoreContextMixin",
]
