"""SQLite-backed durable ledger and engine for managed-thread final delivery.

This module implements the ManagedThreadDeliveryLedger and
ManagedThreadDeliveryEngine protocols from managed_thread_delivery.py backed by
the shared orchestration SQLite database.

Placement:
- The ledger owns all durable state transitions and queries.
- The engine owns orchestration entrypoints (create, claim, record, abandon)
  that combine ledger writes with state-machine policy.
- Surface adapters consume records through the engine, never the other way
  around.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, cast

from ..time_utils import now_iso
from .managed_thread_delivery import (
    MANAGED_THREAD_DELIVERY_TERMINAL_STATES,
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryClaim,
    ManagedThreadDeliveryEnvelope,
    ManagedThreadDeliveryIntent,
    ManagedThreadDeliveryOutcome,
    ManagedThreadDeliveryRecord,
    ManagedThreadDeliveryRecoveryAction,
    ManagedThreadDeliveryRecoverySweepResult,
    ManagedThreadDeliveryRegistration,
    ManagedThreadDeliveryState,
    ManagedThreadDeliveryTarget,
    default_claim_expiry,
    is_valid_managed_thread_delivery_transition,
    normalize_managed_thread_delivery_intent,
    plan_managed_thread_delivery_recovery,
    record_from_intent,
)
from .sqlite import open_orchestration_sqlite

_UNSET = object()
_DEFAULT_CLAIM_TTL = timedelta(minutes=5)
_DEFAULT_RETRY_BACKOFF = timedelta(minutes=1)
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_MULTIPLIER = 2.0
_DEFAULT_MAX_BACKOFF = timedelta(minutes=30)


class SQLiteManagedThreadDeliveryLedger:
    """Durable SQLite-backed implementation of the delivery ledger protocol."""

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable

    def register_intent(
        self, intent: ManagedThreadDeliveryIntent
    ) -> ManagedThreadDeliveryRegistration:
        normalized = normalize_managed_thread_delivery_intent(intent)
        existing = self.get_delivery_by_idempotency_key(normalized.idempotency_key)
        if existing is not None:
            return ManagedThreadDeliveryRegistration(record=existing, inserted=False)
        record = record_from_intent(normalized)
        self._upsert_record(record)
        stored = self.get_delivery(record.delivery_id)
        if stored is None:
            raise RuntimeError(
                f"delivery record missing after insert: {record.delivery_id}"
            )
        return ManagedThreadDeliveryRegistration(record=stored, inserted=True)

    def get_delivery(self, delivery_id: str) -> Optional[ManagedThreadDeliveryRecord]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_managed_thread_deliveries
                 WHERE delivery_id = ?
                """,
                (str(delivery_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def get_delivery_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ManagedThreadDeliveryRecord]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_managed_thread_deliveries
                 WHERE idempotency_key = ?
                """,
                (str(idempotency_key or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)

    def patch_delivery(
        self,
        delivery_id: str,
        *,
        state: ManagedThreadDeliveryState | object = _UNSET,
        validate_transition: bool = True,
        metadata_updates: Optional[Mapping[str, Any]] = None,
        expected_states: Optional[tuple[ManagedThreadDeliveryState, ...]] = None,
        **changes: Any,
    ) -> Optional[ManagedThreadDeliveryRecord]:
        current = self.get_delivery(delivery_id)
        if current is None:
            return None
        if expected_states is not None and current.state not in expected_states:
            return None
        next_state = current.state
        has_state_update = isinstance(state, ManagedThreadDeliveryState)
        if has_state_update:
            next_state = cast(ManagedThreadDeliveryState, state)
        if (
            validate_transition
            and has_state_update
            and next_state != current.state
            and not is_valid_managed_thread_delivery_transition(
                current.state, next_state
            )
        ):
            raise ValueError(
                f"invalid delivery transition: {current.state.value} -> {next_state.value}"
            )
        metadata = dict(current.metadata)
        if metadata_updates:
            metadata.update(dict(metadata_updates))
        updated = replace(
            current,
            state=next_state,
            attempt_count=changes.get("attempt_count", current.attempt_count),
            claim_token=changes.get("claim_token", current.claim_token),
            claimed_at=changes.get("claimed_at", current.claimed_at),
            claim_expires_at=changes.get("claim_expires_at", current.claim_expires_at),
            next_attempt_at=changes.get("next_attempt_at", current.next_attempt_at),
            delivered_at=changes.get("delivered_at", current.delivered_at),
            last_error=changes.get("last_error", current.last_error),
            adapter_cursor=changes.get("adapter_cursor", current.adapter_cursor),
            updated_at=changes.get("updated_at", now_iso()),
            metadata=metadata,
        )
        self._upsert_record(updated)
        return self.get_delivery(delivery_id)

    def list_due_deliveries(
        self,
        *,
        adapter_key: Optional[str] = None,
        now: Optional[str] = None,
        limit: int = 100,
    ) -> list[ManagedThreadDeliveryRecord]:
        params: list[Any] = []
        clauses = [
            "state IN (?, ?)",
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)",
        ]
        params.extend(
            [
                ManagedThreadDeliveryState.PENDING.value,
                ManagedThreadDeliveryState.RETRY_SCHEDULED.value,
            ]
        )
        now_ts = now or now_iso()
        params.append(now_ts)
        if adapter_key is not None:
            clauses.append("adapter_key = ?")
            params.append(str(adapter_key or "").strip())
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_managed_thread_deliveries
                 WHERE {" AND ".join(clauses)}
                 ORDER BY created_at ASC, delivery_id ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def list_records_with_expired_claims(
        self,
        *,
        adapter_key: Optional[str] = None,
        now: Optional[str] = None,
        limit: int = 200,
    ) -> list[ManagedThreadDeliveryRecord]:
        params: list[Any] = []
        clauses = [
            "state IN (?, ?)",
            "claim_expires_at IS NOT NULL",
            "claim_expires_at <= ?",
        ]
        params.extend(
            [
                ManagedThreadDeliveryState.CLAIMED.value,
                ManagedThreadDeliveryState.DELIVERING.value,
            ]
        )
        now_ts = now or now_iso()
        params.append(now_ts)
        if adapter_key is not None:
            clauses.append("adapter_key = ?")
            params.append(str(adapter_key or "").strip())
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_managed_thread_deliveries
                 WHERE {" AND ".join(clauses)}
                 ORDER BY claim_expires_at ASC, created_at ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def list_all_non_terminal_records(
        self,
        *,
        adapter_key: Optional[str] = None,
        limit: int = 500,
    ) -> list[ManagedThreadDeliveryRecord]:
        terminal_values = tuple(
            s.value for s in MANAGED_THREAD_DELIVERY_TERMINAL_STATES
        )
        placeholders = ",".join("?" * len(terminal_values))
        params: list[Any] = list(terminal_values)
        clauses = [f"state NOT IN ({placeholders})"]
        if adapter_key is not None:
            clauses.append("adapter_key = ?")
            params.append(str(adapter_key or "").strip())
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_managed_thread_deliveries
                 WHERE {" AND ".join(clauses)}
                 ORDER BY created_at ASC, delivery_id ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def _upsert_record(self, record: ManagedThreadDeliveryRecord) -> None:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_managed_thread_deliveries (
                        delivery_id,
                        managed_thread_id,
                        managed_turn_id,
                        idempotency_key,
                        surface_kind,
                        adapter_key,
                        surface_key,
                        transport_target_json,
                        envelope_version,
                        final_status,
                        assistant_text,
                        session_notice,
                        error_text,
                        backend_thread_id,
                        token_usage_json,
                        attachments_json,
                        transport_hints_json,
                        envelope_metadata_json,
                        source,
                        state,
                        attempt_count,
                        claim_token,
                        claimed_at,
                        claim_expires_at,
                        next_attempt_at,
                        delivered_at,
                        last_error,
                        adapter_cursor_json,
                        target_metadata_json,
                        record_metadata_json,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(delivery_id) DO UPDATE SET
                        managed_thread_id = excluded.managed_thread_id,
                        managed_turn_id = excluded.managed_turn_id,
                        idempotency_key = excluded.idempotency_key,
                        surface_kind = excluded.surface_kind,
                        adapter_key = excluded.adapter_key,
                        surface_key = excluded.surface_key,
                        transport_target_json = excluded.transport_target_json,
                        envelope_version = excluded.envelope_version,
                        final_status = excluded.final_status,
                        assistant_text = excluded.assistant_text,
                        session_notice = excluded.session_notice,
                        error_text = excluded.error_text,
                        backend_thread_id = excluded.backend_thread_id,
                        token_usage_json = excluded.token_usage_json,
                        attachments_json = excluded.attachments_json,
                        transport_hints_json = excluded.transport_hints_json,
                        envelope_metadata_json = excluded.envelope_metadata_json,
                        source = excluded.source,
                        state = excluded.state,
                        attempt_count = excluded.attempt_count,
                        claim_token = excluded.claim_token,
                        claimed_at = excluded.claimed_at,
                        claim_expires_at = excluded.claim_expires_at,
                        next_attempt_at = excluded.next_attempt_at,
                        delivered_at = excluded.delivered_at,
                        last_error = excluded.last_error,
                        adapter_cursor_json = excluded.adapter_cursor_json,
                        target_metadata_json = excluded.target_metadata_json,
                        record_metadata_json = excluded.record_metadata_json,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    _record_to_row_values(record),
                )


class SQLiteManagedThreadDeliveryEngine:
    """SQLite-backed implementation of the delivery engine protocol."""

    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = True,
        claim_ttl: timedelta = _DEFAULT_CLAIM_TTL,
        retry_backoff: timedelta = _DEFAULT_RETRY_BACKOFF,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_multiplier: float = _DEFAULT_BACKOFF_MULTIPLIER,
        max_backoff: Optional[timedelta] = _DEFAULT_MAX_BACKOFF,
    ) -> None:
        self._ledger = SQLiteManagedThreadDeliveryLedger(hub_root, durable=durable)
        self._claim_ttl = claim_ttl
        self._retry_backoff = retry_backoff
        self._max_attempts = max_attempts
        self._backoff_multiplier = backoff_multiplier
        self._max_backoff = max_backoff

    def create_intent(
        self, intent: ManagedThreadDeliveryIntent
    ) -> ManagedThreadDeliveryRegistration:
        return self._ledger.register_intent(intent)

    def claim_next_delivery(
        self,
        *,
        adapter_key: str,
        now: Optional[datetime] = None,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        current_at = now or datetime.now(timezone.utc)
        due_states = (
            ManagedThreadDeliveryState.PENDING,
            ManagedThreadDeliveryState.RETRY_SCHEDULED,
        )
        for _ in range(64):
            self._recover_expired_claims(
                adapter_key=adapter_key,
                now=current_at,
                limit=25,
            )
            due = self._ledger.list_due_deliveries(
                adapter_key=adapter_key,
                now=current_at.isoformat(),
                limit=1,
            )
            if not due:
                return None
            record = due[0]
            decision = plan_managed_thread_delivery_recovery(
                record,
                now=current_at,
                claim_ttl=self._claim_ttl,
                max_attempts=self._max_attempts,
            )
            if decision.action.value == "abandon":
                abandoned = self._ledger.patch_delivery(
                    record.delivery_id,
                    state=ManagedThreadDeliveryState.ABANDONED,
                    last_error=decision.reason,
                    expected_states=due_states,
                )
                if abandoned is None:
                    continue
                return None
            if decision.action.value not in ("claim", "retry"):
                return None
            claim_token = str(uuid.uuid4())
            claimed_at = current_at.isoformat()
            claim_expires_at = default_claim_expiry(
                claimed_at=current_at, claim_ttl=self._claim_ttl
            )
            claim = self._claim_record(
                record,
                claim_token=claim_token,
                claimed_at=claimed_at,
                claim_expires_at=claim_expires_at,
            )
            if claim is not None:
                return claim
        return None

    def claim_delivery(
        self,
        delivery_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        record = self._ledger.get_delivery(delivery_id)
        if record is None:
            return None
        current_at = now or datetime.now(timezone.utc)
        decision = plan_managed_thread_delivery_recovery(
            record,
            now=current_at,
            claim_ttl=self._claim_ttl,
            max_attempts=self._max_attempts,
        )
        if decision.action.value == "abandon":
            self._ledger.patch_delivery(
                record.delivery_id,
                state=ManagedThreadDeliveryState.ABANDONED,
                last_error=decision.reason,
            )
            return None
        if decision.action.value not in ("claim", "retry"):
            return None
        claim_token = str(uuid.uuid4())
        claimed_at = current_at.isoformat()
        claim_expires_at = default_claim_expiry(
            claimed_at=current_at, claim_ttl=self._claim_ttl
        )
        return self._claim_record(
            record,
            claim_token=claim_token,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
        )

    def ensure_direct_delivery_claim(
        self,
        delivery_id: str,
        *,
        proposed_token: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        """Return a claim suitable for direct-surface delivery bookkeeping.

        Validates *proposed_token* against the ledger when it still matches an
        active lease; otherwise recovers expired claims for this adapter and
        issues a fresh claim (same policy as :meth:`claim_delivery`).
        """
        current_at = now or datetime.now(timezone.utc)
        normalized_id = str(delivery_id or "").strip()
        if not normalized_id:
            return None

        record = self._ledger.get_delivery(normalized_id)
        if record is None:
            return None
        if record.state in MANAGED_THREAD_DELIVERY_TERMINAL_STATES:
            return None

        proposed = str(proposed_token or "").strip()
        if proposed and record.claim_token == proposed:
            decision = plan_managed_thread_delivery_recovery(
                record,
                now=current_at,
                claim_ttl=self._claim_ttl,
                max_attempts=self._max_attempts,
            )
            if (
                decision.action == ManagedThreadDeliveryRecoveryAction.NOOP
                and decision.reason == "claim_active"
            ):
                claimed_at = str(record.claimed_at or current_at.isoformat())
                claim_expires_at = str(
                    record.claim_expires_at
                    or default_claim_expiry(
                        claimed_at=current_at, claim_ttl=self._claim_ttl
                    )
                )
                return ManagedThreadDeliveryClaim(
                    record=record,
                    claim_token=proposed,
                    claimed_at=claimed_at,
                    claim_expires_at=claim_expires_at,
                )

        if proposed and record.claim_token and proposed != record.claim_token:
            if record.state in {
                ManagedThreadDeliveryState.CLAIMED,
                ManagedThreadDeliveryState.DELIVERING,
            }:
                self._ledger.patch_delivery(
                    normalized_id,
                    state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
                    claim_token=None,
                    claimed_at=None,
                    claim_expires_at=None,
                    next_attempt_at=current_at.isoformat(),
                    last_error="direct_delivery_claim_token_mismatch",
                )

        refreshed = self._ledger.get_delivery(normalized_id)
        adapter_key = refreshed.target.adapter_key if refreshed is not None else ""
        if adapter_key:
            self._recover_expired_claims(
                adapter_key=str(adapter_key),
                now=current_at,
                limit=64,
            )
        self._force_delivery_due_for_direct_claim(normalized_id, now=current_at)
        return self.claim_delivery(normalized_id, now=current_at)

    def _force_delivery_due_for_direct_claim(
        self, delivery_id: str, *, now: datetime
    ) -> None:
        """Make PENDING/RETRY_SCHEDULED rows claimable immediately for direct-send."""

        record = self._ledger.get_delivery(delivery_id)
        if record is None:
            return
        if record.state not in {
            ManagedThreadDeliveryState.PENDING,
            ManagedThreadDeliveryState.RETRY_SCHEDULED,
        }:
            return
        due_at = record.next_attempt_at
        if due_at is None:
            return
        parsed = None
        try:
            if str(due_at).endswith("Z"):
                parsed = datetime.fromisoformat(
                    str(due_at).replace("Z", "+00:00")
                )
            else:
                parsed = datetime.fromisoformat(str(due_at))
        except ValueError:
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if parsed <= now:
            return
        self._ledger.patch_delivery(
            delivery_id,
            next_attempt_at=now.isoformat(),
            validate_transition=False,
        )

    def record_attempt_result(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        result: ManagedThreadDeliveryAttemptResult,
    ) -> Optional[ManagedThreadDeliveryRecord]:
        record = self._ledger.get_delivery(delivery_id)
        if record is None:
            return None
        if record.claim_token != claim_token:
            return None
        metadata_updates = dict(result.metadata or {})
        if record.state == ManagedThreadDeliveryState.CLAIMED:
            self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.DELIVERING,
                metadata_updates=metadata_updates or None,
            )
        outcome = result.outcome
        if outcome == ManagedThreadDeliveryOutcome.DELIVERED:
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.DELIVERED,
                delivered_at=now_iso(),
                adapter_cursor=result.adapter_cursor,
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        if outcome == ManagedThreadDeliveryOutcome.DIRECT_SURFACE_DELIVERED:
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.DIRECT_SURFACE_DELIVERED,
                delivered_at=now_iso(),
                adapter_cursor=result.adapter_cursor,
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        if outcome == ManagedThreadDeliveryOutcome.DUPLICATE:
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.DELIVERED,
                delivered_at=now_iso(),
                adapter_cursor=result.adapter_cursor,
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        if outcome == ManagedThreadDeliveryOutcome.RETRY:
            next_attempt_at = _compute_next_attempt_at(
                record.attempt_count,
                self._retry_backoff,
                backoff_multiplier=self._backoff_multiplier,
                max_backoff=self._max_backoff,
            )
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
                next_attempt_at=next_attempt_at,
                last_error=result.error,
                adapter_cursor=result.adapter_cursor,
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        if outcome == ManagedThreadDeliveryOutcome.FAILED:
            if record.attempt_count >= self._max_attempts:
                return self._ledger.patch_delivery(
                    delivery_id,
                    state=ManagedThreadDeliveryState.FAILED,
                    last_error=result.error or "max_attempts_exceeded",
                    claim_token=None,
                    metadata_updates=metadata_updates or None,
                )
            next_attempt_at = _compute_next_attempt_at(
                record.attempt_count,
                self._retry_backoff,
                backoff_multiplier=self._backoff_multiplier,
                max_backoff=self._max_backoff,
            )
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
                next_attempt_at=next_attempt_at,
                last_error=result.error,
                adapter_cursor=result.adapter_cursor,
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        if outcome == ManagedThreadDeliveryOutcome.ABANDONED:
            return self._ledger.patch_delivery(
                delivery_id,
                state=ManagedThreadDeliveryState.ABANDONED,
                last_error=result.error or "abandoned_by_adapter",
                claim_token=None,
                metadata_updates=metadata_updates or None,
            )
        return None

    def abandon_delivery(
        self, delivery_id: str, *, detail: Optional[str] = None
    ) -> Optional[ManagedThreadDeliveryRecord]:
        return self._ledger.patch_delivery(
            delivery_id,
            state=ManagedThreadDeliveryState.ABANDONED,
            last_error=detail or "abandoned_by_policy",
            claim_token=None,
        )

    def recovery_sweep(
        self,
        *,
        adapter_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ManagedThreadDeliveryRecoverySweepResult:
        current_at = now or datetime.now(timezone.utc)
        now_iso_str = current_at.isoformat()
        recovered_claims = 0
        abandoned_exhausted = 0
        due_pending = 0
        due_retries = 0

        expired_claims = self._ledger.list_records_with_expired_claims(
            adapter_key=adapter_key,
            now=now_iso_str,
        )
        for record in expired_claims:
            decision = plan_managed_thread_delivery_recovery(
                record,
                now=current_at,
                claim_ttl=self._claim_ttl,
                max_attempts=self._max_attempts,
            )
            if decision.action.value == "abandon":
                self._ledger.patch_delivery(
                    record.delivery_id,
                    state=ManagedThreadDeliveryState.ABANDONED,
                    last_error=decision.reason,
                    claim_token=None,
                )
                abandoned_exhausted += 1
            elif decision.action.value == "retry":
                next_attempt_at = _compute_next_attempt_at(
                    record.attempt_count,
                    self._retry_backoff,
                    backoff_multiplier=self._backoff_multiplier,
                    max_backoff=self._max_backoff,
                    now=current_at,
                )
                self._ledger.patch_delivery(
                    record.delivery_id,
                    state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
                    next_attempt_at=next_attempt_at,
                    last_error=decision.reason,
                    claim_token=None,
                )
                recovered_claims += 1

        due_records = self._ledger.list_due_deliveries(
            adapter_key=adapter_key,
            now=now_iso_str,
        )
        for record in due_records:
            if record.state == ManagedThreadDeliveryState.PENDING:
                due_pending += 1
            elif record.state == ManagedThreadDeliveryState.RETRY_SCHEDULED:
                due_retries += 1

        total_scanned = len(expired_claims) + len(due_records)
        return ManagedThreadDeliveryRecoverySweepResult(
            recovered_claims=recovered_claims,
            abandoned_exhausted=abandoned_exhausted,
            due_pending=due_pending,
            due_retries=due_retries,
            total_scanned=total_scanned,
        )

    def _claim_record(
        self,
        record: ManagedThreadDeliveryRecord,
        *,
        claim_token: str,
        claimed_at: str,
        claim_expires_at: str,
    ) -> Optional[ManagedThreadDeliveryClaim]:
        updated = self._ledger.patch_delivery(
            record.delivery_id,
            state=ManagedThreadDeliveryState.CLAIMED,
            claim_token=claim_token,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
            attempt_count=record.attempt_count + 1,
            expected_states=(
                ManagedThreadDeliveryState.PENDING,
                ManagedThreadDeliveryState.RETRY_SCHEDULED,
            ),
        )
        if updated is None:
            return None
        return ManagedThreadDeliveryClaim(
            record=updated,
            claim_token=claim_token,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
        )

    def _recover_expired_claims(
        self,
        *,
        adapter_key: str,
        now: datetime,
        limit: int,
    ) -> None:
        expired_claims = self._ledger.list_records_with_expired_claims(
            adapter_key=adapter_key,
            now=now.isoformat(),
            limit=limit,
        )
        for record in expired_claims:
            decision = plan_managed_thread_delivery_recovery(
                record,
                now=now,
                claim_ttl=self._claim_ttl,
                max_attempts=self._max_attempts,
            )
            if decision.action.value == "abandon":
                self._ledger.patch_delivery(
                    record.delivery_id,
                    state=ManagedThreadDeliveryState.ABANDONED,
                    last_error=decision.reason,
                    claim_token=None,
                )
            elif decision.action.value == "retry":
                next_attempt_at = _compute_next_attempt_at(
                    record.attempt_count,
                    self._retry_backoff,
                    backoff_multiplier=self._backoff_multiplier,
                    max_backoff=self._max_backoff,
                    now=now,
                )
                self._ledger.patch_delivery(
                    record.delivery_id,
                    state=ManagedThreadDeliveryState.RETRY_SCHEDULED,
                    next_attempt_at=next_attempt_at,
                    last_error=decision.reason,
                    claim_token=None,
                )


def _compute_next_attempt_at(
    attempt_count: int,
    backoff: timedelta,
    *,
    backoff_multiplier: float = _DEFAULT_BACKOFF_MULTIPLIER,
    max_backoff: Optional[timedelta] = _DEFAULT_MAX_BACKOFF,
    now: Optional[datetime] = None,
) -> str:
    base = now or datetime.now(timezone.utc)
    exponent = max(0, attempt_count - 1)
    delay_seconds = backoff.total_seconds() * (backoff_multiplier**exponent)
    if max_backoff is not None:
        delay_seconds = min(delay_seconds, max_backoff.total_seconds())
    delay = timedelta(seconds=max(0.0, delay_seconds))
    return (base + delay).isoformat()


def _record_to_row_values(record: ManagedThreadDeliveryRecord) -> tuple[Any, ...]:
    envelope = record.envelope
    target = record.target
    return (
        record.delivery_id,
        record.managed_thread_id,
        record.managed_turn_id,
        record.idempotency_key,
        target.surface_kind,
        target.adapter_key,
        target.surface_key,
        json.dumps(dict(target.transport_target or {}), sort_keys=True),
        envelope.envelope_version,
        envelope.final_status,
        envelope.assistant_text,
        envelope.session_notice,
        envelope.error_text,
        envelope.backend_thread_id,
        (
            json.dumps(dict(envelope.token_usage or {}), sort_keys=True)
            if envelope.token_usage
            else None
        ),
        json.dumps(
            [dict(a.__dict__) for a in (envelope.attachments or ())], sort_keys=True
        ),
        json.dumps(dict(envelope.transport_hints or {}), sort_keys=True),
        json.dumps(dict(envelope.metadata or {}), sort_keys=True),
        record.source,
        record.state.value,
        max(0, int(record.attempt_count or 0)),
        record.claim_token,
        record.claimed_at,
        record.claim_expires_at,
        record.next_attempt_at,
        record.delivered_at,
        record.last_error,
        (
            json.dumps(dict(record.adapter_cursor or {}), sort_keys=True)
            if isinstance(record.adapter_cursor, Mapping)
            else None
        ),
        json.dumps(dict(target.metadata or {}), sort_keys=True),
        json.dumps(dict(record.metadata or {}), sort_keys=True),
        record.created_at or now_iso(),
        record.updated_at or now_iso(),
    )


def _record_from_row(row: Any) -> ManagedThreadDeliveryRecord:
    attachments_raw = _decode_json(row["attachments_json"], default=[])
    attachments = tuple(
        _attachment_from_dict(a)
        for a in (attachments_raw if isinstance(attachments_raw, list) else [])
    )
    target = ManagedThreadDeliveryTarget(
        surface_kind=str(row["surface_kind"]),
        adapter_key=str(row["adapter_key"]),
        surface_key=str(row["surface_key"]),
        transport_target=_decode_json(row["transport_target_json"]),
        metadata=_decode_json(row["target_metadata_json"]),
    )
    envelope = ManagedThreadDeliveryEnvelope(
        envelope_version=str(row["envelope_version"]),
        final_status=str(row["final_status"]),
        assistant_text=str(row["assistant_text"]),
        session_notice=_optional_text(row["session_notice"]),
        error_text=_optional_text(row["error_text"]),
        backend_thread_id=_optional_text(row["backend_thread_id"]),
        token_usage=_decode_json_or_none(row["token_usage_json"]),
        attachments=attachments,
        transport_hints=_decode_json(row["transport_hints_json"]),
        metadata=_decode_json(row["envelope_metadata_json"]),
    )
    adapter_cursor = _decode_json(row["adapter_cursor_json"])
    return ManagedThreadDeliveryRecord(
        delivery_id=str(row["delivery_id"]),
        managed_thread_id=str(row["managed_thread_id"]),
        managed_turn_id=str(row["managed_turn_id"]),
        idempotency_key=str(row["idempotency_key"]),
        target=target,
        envelope=envelope,
        state=ManagedThreadDeliveryState(str(row["state"])),
        source=str(row["source"]),
        attempt_count=max(0, int(row["attempt_count"] or 0)),
        claim_token=_optional_text(row["claim_token"]),
        claimed_at=_optional_text(row["claimed_at"]),
        claim_expires_at=_optional_text(row["claim_expires_at"]),
        next_attempt_at=_optional_text(row["next_attempt_at"]),
        delivered_at=_optional_text(row["delivered_at"]),
        last_error=_optional_text(row["last_error"]),
        adapter_cursor=adapter_cursor if adapter_cursor else None,
        created_at=_optional_text(row["created_at"]),
        updated_at=_optional_text(row["updated_at"]),
        metadata=_decode_json(row["record_metadata_json"]),
    )


def _attachment_from_dict(data: Any) -> Any:
    from .managed_thread_delivery import ManagedThreadDeliveryAttachment

    if not isinstance(data, dict):
        return ManagedThreadDeliveryAttachment(attachment_id=str(data))
    return ManagedThreadDeliveryAttachment(
        attachment_id=str(data.get("attachment_id", "")),
        kind=str(data.get("kind", "file")),
        path=data.get("path"),
        mime_type=data.get("mime_type"),
        caption=data.get("caption"),
        metadata=data.get("metadata", {}),
    )


def _decode_json(raw: Any, default: Any = None) -> Any:
    if not isinstance(raw, str) or not raw:
        return default if default is not None else {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return default if default is not None else {}
    return parsed


def _decode_json_or_none(raw: Any) -> Optional[dict[str, Any]]:
    result = _decode_json(raw)
    if isinstance(result, dict) and result:
        return result
    return None


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "SQLiteManagedThreadDeliveryEngine",
    "SQLiteManagedThreadDeliveryLedger",
]
