from __future__ import annotations

from typing import Callable

from .pma_automation_records import (
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
)
from .pma_automation_types import _iso_now
from .pma_domain.models import PmaSubscription, PmaTimer, PmaWakeup
from .pma_domain.subscription_reducer import ReduceTransitionResult


class PmaAutomationDomainTranslator:
    @staticmethod
    def _lifecycle_sub_to_domain(
        entry: PmaLifecycleSubscription,
    ) -> PmaSubscription:
        return PmaSubscription(
            subscription_id=entry.subscription_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            event_types=tuple(entry.event_types),
            repo_id=entry.repo_id,
            run_id=entry.run_id,
            thread_id=entry.thread_id,
            lane_id=entry.lane_id,
            from_state=entry.from_state,
            to_state=entry.to_state,
            reason=entry.reason,
            idempotency_key=entry.idempotency_key,
            max_matches=entry.max_matches,
            match_count=entry.match_count,
            metadata=dict(entry.metadata),
        )

    @staticmethod
    def _store_timer_to_domain(entry: PmaAutomationTimer) -> PmaTimer:
        return PmaTimer(
            timer_id=entry.timer_id,
            due_at=entry.due_at,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            fired_at=entry.fired_at,
            timer_type=entry.timer_type,
            idle_seconds=entry.idle_seconds,
            subscription_id=entry.subscription_id,
            repo_id=entry.repo_id,
            run_id=entry.run_id,
            thread_id=entry.thread_id,
            lane_id=entry.lane_id,
            from_state=entry.from_state,
            to_state=entry.to_state,
            reason=entry.reason,
            idempotency_key=entry.idempotency_key,
            metadata=dict(entry.metadata or {}),
        )

    @staticmethod
    def _store_wakeup_to_domain(entry: PmaAutomationWakeup) -> PmaWakeup:
        return PmaWakeup(
            wakeup_id=entry.wakeup_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            state=entry.state,
            dispatched_at=entry.dispatched_at,
            source=entry.source,
            repo_id=entry.repo_id,
            run_id=entry.run_id,
            thread_id=entry.thread_id,
            lane_id=entry.lane_id,
            from_state=entry.from_state,
            to_state=entry.to_state,
            reason=entry.reason,
            timestamp=entry.timestamp,
            idempotency_key=entry.idempotency_key,
            subscription_id=entry.subscription_id,
            timer_id=entry.timer_id,
            event_id=entry.event_id,
            event_type=entry.event_type,
            event_data=dict(entry.event_data or {}),
            metadata=dict(entry.metadata or {}),
        )

    @staticmethod
    def _apply_domain_timer_to_store(
        store_timer: PmaAutomationTimer, domain_timer: PmaTimer
    ) -> None:
        store_timer.due_at = domain_timer.due_at
        store_timer.updated_at = domain_timer.updated_at
        store_timer.state = domain_timer.state
        store_timer.fired_at = domain_timer.fired_at
        store_timer.idle_seconds = domain_timer.idle_seconds
        store_timer.reason = domain_timer.reason
        store_timer.metadata = dict(domain_timer.metadata or {})

    @staticmethod
    def _apply_domain_wakeup_to_store(
        store_wakeup: PmaAutomationWakeup, domain_wakeup: PmaWakeup
    ) -> None:
        store_wakeup.state = domain_wakeup.state
        store_wakeup.dispatched_at = domain_wakeup.dispatched_at
        store_wakeup.updated_at = domain_wakeup.updated_at
        store_wakeup.metadata = dict(domain_wakeup.metadata or {})

    @staticmethod
    def _apply_reduce_result(
        subscriptions: list[PmaLifecycleSubscription],
        wakeups: list[PmaAutomationWakeup],
        result: ReduceTransitionResult,
        timestamp: str,
        *,
        compute_dispatch: Callable[[PmaAutomationWakeup], None] | None = None,
    ) -> list[PmaAutomationWakeup]:
        sub_by_id = {entry.subscription_id: entry for entry in subscriptions}
        now = _iso_now()
        for domain_sub in result.subscriptions:
            existing = sub_by_id.get(domain_sub.subscription_id)
            if existing is None:
                continue
            if existing.match_count != domain_sub.match_count:
                existing.match_count = domain_sub.match_count
                existing.updated_at = now
            if existing.state != domain_sub.state:
                existing.state = domain_sub.state
                existing.updated_at = now
        appended: list[PmaAutomationWakeup] = []
        for intent in result.wakeup_intents:
            wakeup = PmaAutomationWakeup.create(
                source=intent.source,
                repo_id=intent.repo_id,
                run_id=intent.run_id,
                thread_id=intent.thread_id,
                lane_id=intent.lane_id,
                from_state=intent.from_state,
                to_state=intent.to_state,
                reason=intent.reason,
                timestamp=timestamp,
                idempotency_key=intent.idempotency_key,
                subscription_id=intent.subscription_id,
                event_type=intent.event_type,
                event_id=intent.event_id,
                event_data=intent.event_data,
                metadata=intent.metadata,
            )
            if compute_dispatch is not None:
                compute_dispatch(wakeup)
            wakeups.append(wakeup)
            appended.append(wakeup)
        return appended


__all__ = ["PmaAutomationDomainTranslator"]
