"""Tests for the managed-thread delivery replay and recovery worker.

These tests prove the worker correctly claims due records, hands them to
adapters, records results, and runs recovery sweeps through the runtime
lifecycle hooks — covering restart replay, retry-scheduled delivery, and
expired-claim recovery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from codex_autorunner.core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryState,
    SQLiteManagedThreadDeliveryEngine,
    initialize_orchestration_sqlite,
)
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryTarget,
    build_managed_thread_delivery_id,
    build_managed_thread_delivery_idempotency_key,
)
from codex_autorunner.integrations.chat.managed_thread_delivery_worker import (
    ManagedThreadDeliveryWorker,
    ManagedThreadDeliveryWorkerConfig,
)


def _make_engine(
    tmp_path: Path,
    *,
    retry_backoff_seconds: int = 0,
    max_attempts: int = 5,
) -> SQLiteManagedThreadDeliveryEngine:
    hub_root = tmp_path / "hub"
    initialize_orchestration_sqlite(hub_root, durable=False)
    return SQLiteManagedThreadDeliveryEngine(
        hub_root,
        durable=False,
        retry_backoff=timedelta(seconds=retry_backoff_seconds),
        max_attempts=max_attempts,
    )


def _make_target(
    *,
    adapter_key: str = "test",
    surface_key: str = "surface-1",
    channel_id: str = "ch-1",
) -> ManagedThreadDeliveryTarget:
    return ManagedThreadDeliveryTarget(
        surface_kind=adapter_key,
        adapter_key=adapter_key,
        surface_key=surface_key,
        transport_target={"channel_id": channel_id},
    )


def _make_envelope(
    *,
    final_status: str = "ok",
    assistant_text: str = "Hello!",
    error_text: Optional[str] = None,
) -> ManagedThreadDeliveryEnvelope:
    return ManagedThreadDeliveryEnvelope(
        envelope_version="1",
        final_status=final_status,
        assistant_text=assistant_text,
        error_text=error_text,
    )


def _register_pending(
    engine: SQLiteManagedThreadDeliveryEngine,
    *,
    adapter_key: str = "test",
    surface_key: str = "surface-1",
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    final_status: str = "ok",
    assistant_text: str = "Hello!",
) -> str:
    target = _make_target(adapter_key=adapter_key, surface_key=surface_key)
    envelope = _make_envelope(final_status=final_status, assistant_text=assistant_text)
    delivery_id = build_managed_thread_delivery_id(
        managed_thread_id=thread_id,
        managed_turn_id=turn_id,
        surface_kind=adapter_key,
        surface_key=surface_key,
    )
    idempotency_key = build_managed_thread_delivery_idempotency_key(
        managed_thread_id=thread_id,
        managed_turn_id=turn_id,
        surface_kind=adapter_key,
        surface_key=surface_key,
    )
    intent = ManagedThreadDeliveryIntent(
        delivery_id=delivery_id,
        managed_thread_id=thread_id,
        managed_turn_id=turn_id,
        idempotency_key=idempotency_key,
        target=target,
        envelope=envelope,
        not_before=datetime.now(timezone.utc).isoformat(),
    )
    reg = engine.create_intent(intent)
    return reg.record.delivery_id


class _RecordingAdapter:
    def __init__(
        self,
        *,
        adapter_key: str = "test",
        outcomes: Optional[list[ManagedThreadDeliveryOutcome]] = None,
    ) -> None:
        self._adapter_key = adapter_key
        self._outcomes = list(outcomes or [ManagedThreadDeliveryOutcome.DELIVERED])
        self._call_index = 0
        self.delivered_records: list[ManagedThreadDeliveryIntent] = []

    @property
    def adapter_key(self) -> str:
        return self._adapter_key

    async def deliver_managed_thread_record(
        self, record: Any, *, claim: Any
    ) -> ManagedThreadDeliveryAttemptResult:
        self.delivered_records.append(record)
        outcome = (
            self._outcomes[self._call_index]
            if self._call_index < len(self._outcomes)
            else self._outcomes[-1]
        )
        self._call_index += 1
        return ManagedThreadDeliveryAttemptResult(outcome=outcome)


@pytest.mark.anyio
async def test_worker_claims_and_delivers_pending_record(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    adapter = _RecordingAdapter()
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )
    delivery_id = _register_pending(engine)

    await worker.run_once()

    assert len(adapter.delivered_records) == 1
    assert adapter.delivered_records[0].delivery_id == delivery_id
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.DELIVERED
    assert worker.stats.claims_processed == 1
    assert worker.stats.deliveries_succeeded == 1


@pytest.mark.anyio
async def test_worker_skips_when_no_due_records(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    adapter = _RecordingAdapter()
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )

    await worker.run_once()

    assert len(adapter.delivered_records) == 0
    assert worker.stats.claims_processed == 0


@pytest.mark.anyio
async def test_worker_retries_on_adapter_failure(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, retry_backoff_seconds=0)
    adapter = _RecordingAdapter(
        outcomes=[
            ManagedThreadDeliveryOutcome.FAILED,
            ManagedThreadDeliveryOutcome.DELIVERED,
        ]
    )
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )
    delivery_id = _register_pending(engine)

    await worker.run_once()
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.RETRY_SCHEDULED

    await worker.run_once()
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.DELIVERED

    assert worker.stats.claims_processed == 2
    assert worker.stats.deliveries_failed == 1
    assert worker.stats.deliveries_succeeded == 1


@pytest.mark.anyio
async def test_worker_handles_adapter_timeout(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, retry_backoff_seconds=0)
    _register_pending(engine)

    class _SlowAdapter:
        @property
        def adapter_key(self) -> str:
            return "test"

        async def deliver_managed_thread_record(
            self, record: Any, *, claim: Any
        ) -> ManagedThreadDeliveryAttemptResult:
            await asyncio.sleep(10)
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.DELIVERED
            )

    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=_SlowAdapter(),
        logger=logging.getLogger("test"),
        config=ManagedThreadDeliveryWorkerConfig(adapter_timeout_seconds=0.01),
    )

    await worker.run_once()

    assert worker.stats.claims_processed == 1
    assert worker.stats.deliveries_retried == 1


@pytest.mark.anyio
async def test_worker_recovery_sweep_reclaims_expired_claims(
    tmp_path: Path,
) -> None:
    engine = _make_engine(tmp_path, retry_backoff_seconds=0)
    delivery_id = _register_pending(engine)

    claim = engine.claim_delivery(delivery_id)
    assert claim is not None

    engine._ledger.patch_delivery(
        delivery_id,
        state=ManagedThreadDeliveryState.DELIVERING,
        claim_expires_at=(
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(),
    )

    adapter = _RecordingAdapter()
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
        config=ManagedThreadDeliveryWorkerConfig(recovery_interval_ticks=1),
    )

    await worker.run_once()

    assert worker.stats.recovery_sweeps == 1
    assert worker.stats.last_recovery_result is not None
    assert worker.stats.last_recovery_result.recovered_claims >= 1


@pytest.mark.anyio
async def test_worker_abandons_exhausted_records(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, retry_backoff_seconds=0, max_attempts=2)
    delivery_id = _register_pending(engine)

    adapter = _RecordingAdapter(
        outcomes=[
            ManagedThreadDeliveryOutcome.FAILED,
            ManagedThreadDeliveryOutcome.FAILED,
        ]
    )
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )

    await worker.run_once()
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.RETRY_SCHEDULED

    await worker.run_once()
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.FAILED

    assert worker.stats.deliveries_failed == 2
    assert worker.stats.deliveries_abandoned == 0


@pytest.mark.anyio
async def test_worker_loop_cancels_cleanly(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    adapter = _RecordingAdapter()
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
        config=ManagedThreadDeliveryWorkerConfig(poll_interval_seconds=0.01),
    )

    task = asyncio.create_task(worker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.stats.errors == 0


@pytest.mark.anyio
async def test_restart_replay_delivers_persisted_record(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, retry_backoff_seconds=0)
    delivery_id = _register_pending(engine)

    claim = engine.claim_delivery(delivery_id)
    assert claim is not None
    engine._ledger.patch_delivery(
        delivery_id,
        state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
        next_attempt_at=datetime.now(timezone.utc).isoformat(),
        claim_token=None,
    )

    adapter = _RecordingAdapter()
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )

    await worker.run_once()

    assert len(adapter.delivered_records) == 1
    assert adapter.delivered_records[0].delivery_id == delivery_id
    record = engine._ledger.get_delivery(delivery_id)
    assert record is not None
    assert record.state == ManagedThreadDeliveryState.DELIVERED


@pytest.mark.anyio
async def test_worker_scopes_claims_to_adapter_key(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    _register_pending(engine, adapter_key="test", surface_key="s1")
    _register_pending(engine, adapter_key="other", surface_key="s2")

    adapter = _RecordingAdapter(adapter_key="test")
    worker = ManagedThreadDeliveryWorker(
        engine=engine,
        adapter=adapter,
        logger=logging.getLogger("test"),
    )

    await worker.run_once()

    assert len(adapter.delivered_records) == 1
    assert adapter.delivered_records[0].target.adapter_key == "test"
