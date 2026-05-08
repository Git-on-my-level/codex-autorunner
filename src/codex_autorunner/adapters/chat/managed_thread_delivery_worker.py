"""Protocol-agnostic replay and recovery worker for durable managed-thread delivery.

This module provides the production loop that runs inside Discord and Telegram
runtime lifecycles. On each tick the worker:

1. Runs a recovery sweep to reclaim expired claims and abandon exhausted records.
2. Claims the next due delivery record via the engine.
3. Hands the claimed record to the surface adapter for transport.
4. Records the adapter result back into the engine.

The worker never decides retry policy or terminal state — that belongs to the
engine. The worker is purely an executor that bridges the engine and adapter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ...core.logging_utils import log_event
from ...core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryEngine,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecoverySweepResult,
)
from .managed_thread_delivery import ManagedThreadDeliveryAdapter

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_RECOVERY_INTERVAL_TICKS = 12
_DEFAULT_ADAPTER_TIMEOUT_SECONDS = 120.0


@dataclass
class ManagedThreadDeliveryWorkerStats:
    claims_processed: int = 0
    deliveries_succeeded: int = 0
    deliveries_failed: int = 0
    deliveries_retried: int = 0
    deliveries_abandoned: int = 0
    recovery_sweeps: int = 0
    last_recovery_result: Optional[ManagedThreadDeliveryRecoverySweepResult] = None
    errors: int = 0


@dataclass
class ManagedThreadDeliveryWorkerConfig:
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS
    recovery_interval_ticks: int = _DEFAULT_RECOVERY_INTERVAL_TICKS
    adapter_timeout_seconds: float = _DEFAULT_ADAPTER_TIMEOUT_SECONDS


class ManagedThreadDeliveryWorker:
    """Protocol-agnostic delivery replay and recovery worker."""

    def __init__(
        self,
        *,
        engine: ManagedThreadDeliveryEngine,
        adapter: ManagedThreadDeliveryAdapter,
        logger: logging.Logger,
        config: Optional[ManagedThreadDeliveryWorkerConfig] = None,
    ) -> None:
        self._engine = engine
        self._adapter = adapter
        self._logger = logger
        self._config = config or ManagedThreadDeliveryWorkerConfig()
        self._stats = ManagedThreadDeliveryWorkerStats()
        self._tick_count: int = 0

    @property
    def stats(self) -> ManagedThreadDeliveryWorkerStats:
        return self._stats

    @property
    def adapter_key(self) -> str:
        return self._adapter.adapter_key

    async def run_once(self) -> None:
        """Execute one claim-deliver-record cycle, plus recovery if due."""
        self._tick_count += 1
        if self._tick_count % self._config.recovery_interval_ticks == 0:
            await self._run_recovery_sweep()
        await self._claim_and_deliver_one()

    async def run_loop(self) -> None:
        """Run the worker loop until cancelled."""
        log_event(
            self._logger,
            logging.INFO,
            "chat.managed_thread.delivery_worker.started",
            adapter_key=self._adapter.adapter_key,
            poll_interval_seconds=self._config.poll_interval_seconds,
            recovery_interval_ticks=self._config.recovery_interval_ticks,
        )
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stats.errors += 1
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "chat.managed_thread.delivery_worker.tick_failed",
                        adapter_key=self._adapter.adapter_key,
                        exc=exc,
                    )
                await asyncio.sleep(self._config.poll_interval_seconds)
        finally:
            log_event(
                self._logger,
                logging.INFO,
                "chat.managed_thread.delivery_worker.stopped",
                adapter_key=self._adapter.adapter_key,
                claims_processed=self._stats.claims_processed,
                deliveries_succeeded=self._stats.deliveries_succeeded,
                deliveries_failed=self._stats.deliveries_failed,
                deliveries_retried=self._stats.deliveries_retried,
                deliveries_abandoned=self._stats.deliveries_abandoned,
                errors=self._stats.errors,
            )

    async def _claim_and_deliver_one(self) -> None:
        claim = self._engine.claim_next_delivery(adapter_key=self._adapter.adapter_key)
        if claim is None:
            return
        self._stats.claims_processed += 1
        record = claim.record
        log_event(
            self._logger,
            logging.INFO,
            "chat.managed_thread.delivery_worker.claimed",
            delivery_id=record.delivery_id,
            managed_thread_id=record.managed_thread_id,
            managed_turn_id=record.managed_turn_id,
            adapter_key=self._adapter.adapter_key,
            attempt_count=record.attempt_count,
        )
        try:
            try:
                result = await asyncio.wait_for(
                    self._adapter.deliver_managed_thread_record(record, claim=claim),
                    timeout=self._config.adapter_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = ManagedThreadDeliveryAttemptResult(
                    outcome=ManagedThreadDeliveryOutcome.RETRY,
                    error="adapter_delivery_timeout",
                )
        except asyncio.CancelledError:
            _record_attempt_safely(
                self._engine,
                record.delivery_id,
                claim.claim_token,
                ManagedThreadDeliveryAttemptResult(
                    outcome=ManagedThreadDeliveryOutcome.RETRY,
                    error="delivery_worker_cancelled",
                ),
                self._logger,
                adapter_key=self._adapter.adapter_key,
            )
            raise
        except Exception as exc:
            result = ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=str(exc) or exc.__class__.__name__,
            )

        _record_attempt_safely(
            self._engine,
            record.delivery_id,
            claim.claim_token,
            result,
            self._logger,
            adapter_key=self._adapter.adapter_key,
        )
        self._update_stats_for_outcome(result)

    async def _run_recovery_sweep(self) -> None:
        try:
            sweep_result = self._engine.recovery_sweep(
                adapter_key=self._adapter.adapter_key
            )
        except Exception as exc:
            self._stats.errors += 1
            log_event(
                self._logger,
                logging.WARNING,
                "chat.managed_thread.delivery_worker.recovery_sweep_failed",
                adapter_key=self._adapter.adapter_key,
                exc=exc,
            )
            return
        self._stats.recovery_sweeps += 1
        self._stats.last_recovery_result = sweep_result
        if sweep_result.total_scanned > 0:
            log_event(
                self._logger,
                logging.INFO,
                "chat.managed_thread.delivery_worker.recovery_sweep",
                adapter_key=self._adapter.adapter_key,
                recovered_claims=sweep_result.recovered_claims,
                abandoned_exhausted=sweep_result.abandoned_exhausted,
                due_pending=sweep_result.due_pending,
                due_retries=sweep_result.due_retries,
                total_scanned=sweep_result.total_scanned,
            )

    def _update_stats_for_outcome(
        self, result: ManagedThreadDeliveryAttemptResult
    ) -> None:
        outcome = result.outcome
        if outcome in (
            ManagedThreadDeliveryOutcome.DELIVERED,
            ManagedThreadDeliveryOutcome.DUPLICATE,
        ):
            self._stats.deliveries_succeeded += 1
        elif outcome == ManagedThreadDeliveryOutcome.FAILED:
            self._stats.deliveries_failed += 1
        elif outcome == ManagedThreadDeliveryOutcome.RETRY:
            self._stats.deliveries_retried += 1
        elif outcome == ManagedThreadDeliveryOutcome.ABANDONED:
            self._stats.deliveries_abandoned += 1


def _record_attempt_safely(
    engine: ManagedThreadDeliveryEngine,
    delivery_id: str,
    claim_token: str,
    result: ManagedThreadDeliveryAttemptResult,
    logger: logging.Logger,
    *,
    adapter_key: str,
) -> None:
    try:
        engine.record_attempt_result(
            delivery_id, claim_token=claim_token, result=result
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "chat.managed_thread.delivery_worker.record_result_failed",
            delivery_id=delivery_id,
            adapter_key=adapter_key,
            outcome=result.outcome.value,
            attempted_error=result.error,
            exc=exc,
        )


__all__ = [
    "ManagedThreadDeliveryWorker",
    "ManagedThreadDeliveryWorkerConfig",
    "ManagedThreadDeliveryWorkerStats",
]
