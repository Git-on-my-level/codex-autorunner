"""Durable post-terminal side-effect ledger for managed-thread turns."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from ..time_utils import now_iso
from .sqlite import open_orchestration_sqlite

_DEFAULT_CLAIM_TTL = timedelta(minutes=5)
_DEFAULT_RETRY_BACKOFF = timedelta(minutes=1)
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_MAX_BACKOFF = timedelta(minutes=30)


class ManagedThreadSideEffectState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES = frozenset(
    {
        ManagedThreadSideEffectState.SUCCEEDED,
        ManagedThreadSideEffectState.FAILED,
        ManagedThreadSideEffectState.ABANDONED,
    }
)


class ManagedThreadSideEffectOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class ManagedThreadSideEffectIntent:
    effect_id: str
    managed_thread_id: str
    managed_turn_id: str
    idempotency_key: str
    effect_kind: str
    surface_kind: str
    surface_key: str
    payload: Mapping[str, Any]
    created_at: Optional[str] = None
    not_before: Optional[str] = None


@dataclass(frozen=True)
class ManagedThreadSideEffectRecord:
    effect_id: str
    managed_thread_id: str
    managed_turn_id: str
    idempotency_key: str
    effect_kind: str
    surface_kind: str
    surface_key: str
    payload: Mapping[str, Any]
    state: ManagedThreadSideEffectState
    attempt_count: int = 0
    claim_token: Optional[str] = None
    claimed_at: Optional[str] = None
    claim_expires_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class ManagedThreadSideEffectRegistration:
    record: ManagedThreadSideEffectRecord
    inserted: bool


@dataclass(frozen=True)
class ManagedThreadSideEffectClaim:
    record: ManagedThreadSideEffectRecord
    claim_token: str


@dataclass(frozen=True)
class ManagedThreadSideEffectAttemptResult:
    outcome: ManagedThreadSideEffectOutcome
    error: Optional[str] = None


def build_managed_thread_side_effect_idempotency_key(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    surface_kind: str,
    surface_key: str,
    effect_kind: str,
) -> str:
    return ":".join(
        (
            "managed-thread-side-effect",
            str(managed_thread_id or "").strip(),
            str(managed_turn_id or "").strip(),
            str(surface_kind or "").strip(),
            str(surface_key or "").strip(),
            str(effect_kind or "").strip(),
        )
    )


def build_managed_thread_side_effect_id(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    surface_kind: str,
    surface_key: str,
    effect_kind: str,
) -> str:
    seed = build_managed_thread_side_effect_idempotency_key(
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        surface_kind=surface_kind,
        surface_key=surface_key,
        effect_kind=effect_kind,
    )
    return "mtse-" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex


class SQLiteManagedThreadSideEffectLedger:
    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable

    def register_intent(
        self, intent: ManagedThreadSideEffectIntent
    ) -> ManagedThreadSideEffectRegistration:
        existing = self.get_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            return ManagedThreadSideEffectRegistration(existing, inserted=False)
        now = now_iso()
        record = ManagedThreadSideEffectRecord(
            effect_id=str(intent.effect_id or "").strip(),
            managed_thread_id=str(intent.managed_thread_id or "").strip(),
            managed_turn_id=str(intent.managed_turn_id or "").strip(),
            idempotency_key=str(intent.idempotency_key or "").strip(),
            effect_kind=str(intent.effect_kind or "").strip(),
            surface_kind=str(intent.surface_kind or "").strip(),
            surface_key=str(intent.surface_key or "").strip(),
            payload=dict(intent.payload or {}),
            state=ManagedThreadSideEffectState.PENDING,
            next_attempt_at=intent.not_before,
            created_at=intent.created_at or now,
            updated_at=now,
        )
        self._upsert(record)
        stored = self.get_effect(record.effect_id)
        if stored is None:
            raise RuntimeError(
                f"side-effect record missing after insert: {record.effect_id}"
            )
        return ManagedThreadSideEffectRegistration(stored, inserted=True)

    def get_effect(self, effect_id: str) -> Optional[ManagedThreadSideEffectRecord]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                "SELECT * FROM orch_managed_thread_side_effects WHERE effect_id = ?",
                (str(effect_id or "").strip(),),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ManagedThreadSideEffectRecord]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                "SELECT * FROM orch_managed_thread_side_effects WHERE idempotency_key = ?",
                (str(idempotency_key or "").strip(),),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list_due(
        self,
        *,
        effect_kind: Optional[str] = None,
        now: Optional[str] = None,
        limit: int = 100,
    ) -> list[ManagedThreadSideEffectRecord]:
        params: list[Any] = [
            ManagedThreadSideEffectState.PENDING.value,
            ManagedThreadSideEffectState.RETRY_SCHEDULED.value,
            now or now_iso(),
        ]
        clauses = [
            "state IN (?, ?)",
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)",
        ]
        if effect_kind is not None:
            clauses.append("effect_kind = ?")
            params.append(str(effect_kind or "").strip())
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_managed_thread_side_effects
                 WHERE {" AND ".join(clauses)}
                 ORDER BY created_at ASC, effect_id ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def patch(
        self,
        effect_id: str,
        *,
        state: Optional[ManagedThreadSideEffectState] = None,
        expected_states: Optional[tuple[ManagedThreadSideEffectState, ...]] = None,
        **changes: Any,
    ) -> Optional[ManagedThreadSideEffectRecord]:
        current = self.get_effect(effect_id)
        if current is None:
            return None
        if expected_states is not None and current.state not in expected_states:
            return None
        updated = replace(
            current,
            state=state or current.state,
            attempt_count=changes.get("attempt_count", current.attempt_count),
            claim_token=changes.get("claim_token", current.claim_token),
            claimed_at=changes.get("claimed_at", current.claimed_at),
            claim_expires_at=changes.get("claim_expires_at", current.claim_expires_at),
            next_attempt_at=changes.get("next_attempt_at", current.next_attempt_at),
            completed_at=changes.get("completed_at", current.completed_at),
            last_error=changes.get("last_error", current.last_error),
            updated_at=changes.get("updated_at", now_iso()),
        )
        self._upsert(updated)
        return self.get_effect(effect_id)

    def _upsert(self, record: ManagedThreadSideEffectRecord) -> None:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_managed_thread_side_effects (
                        effect_id, managed_thread_id, managed_turn_id, idempotency_key,
                        effect_kind, surface_kind, surface_key, payload_json, state,
                        attempt_count, claim_token, claimed_at, claim_expires_at,
                        next_attempt_at, completed_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(effect_id) DO UPDATE SET
                        state = excluded.state,
                        attempt_count = excluded.attempt_count,
                        claim_token = excluded.claim_token,
                        claimed_at = excluded.claimed_at,
                        claim_expires_at = excluded.claim_expires_at,
                        next_attempt_at = excluded.next_attempt_at,
                        completed_at = excluded.completed_at,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    _record_to_row_values(record),
                )


class SQLiteManagedThreadSideEffectEngine:
    def __init__(
        self,
        hub_root: Path,
        *,
        durable: bool = True,
        claim_ttl: timedelta = _DEFAULT_CLAIM_TTL,
        retry_backoff: timedelta = _DEFAULT_RETRY_BACKOFF,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._ledger = SQLiteManagedThreadSideEffectLedger(hub_root, durable=durable)
        self._claim_ttl = claim_ttl
        self._retry_backoff = retry_backoff
        self._max_attempts = max_attempts

    def create_intent(
        self, intent: ManagedThreadSideEffectIntent
    ) -> ManagedThreadSideEffectRegistration:
        return self._ledger.register_intent(intent)

    def claim_effect(
        self, effect_id: str, *, now: Optional[datetime] = None
    ) -> Optional[ManagedThreadSideEffectClaim]:
        record = self._ledger.get_effect(effect_id)
        if record is None or record.state in MANAGED_THREAD_SIDE_EFFECT_TERMINAL_STATES:
            return None
        if record.state not in {
            ManagedThreadSideEffectState.PENDING,
            ManagedThreadSideEffectState.RETRY_SCHEDULED,
        }:
            return None
        current_at = now or datetime.now(timezone.utc)
        token = str(uuid.uuid4())
        updated = self._ledger.patch(
            effect_id,
            state=ManagedThreadSideEffectState.CLAIMED,
            claim_token=token,
            claimed_at=current_at.isoformat(),
            claim_expires_at=(current_at + self._claim_ttl).isoformat(),
            attempt_count=record.attempt_count + 1,
            expected_states=(
                ManagedThreadSideEffectState.PENDING,
                ManagedThreadSideEffectState.RETRY_SCHEDULED,
            ),
        )
        if updated is None:
            return None
        return ManagedThreadSideEffectClaim(record=updated, claim_token=token)

    def claim_next(
        self, *, effect_kind: Optional[str] = None, now: Optional[datetime] = None
    ) -> Optional[ManagedThreadSideEffectClaim]:
        current_at = now or datetime.now(timezone.utc)
        self.recover_expired_claims(now=current_at)
        for record in self._ledger.list_due(
            effect_kind=effect_kind,
            now=current_at.isoformat(),
            limit=25,
        ):
            claim = self.claim_effect(record.effect_id, now=current_at)
            if claim is not None:
                return claim
        return None

    def record_attempt_result(
        self,
        effect_id: str,
        *,
        claim_token: str,
        result: ManagedThreadSideEffectAttemptResult,
    ) -> Optional[ManagedThreadSideEffectRecord]:
        record = self._ledger.get_effect(effect_id)
        if record is None or record.claim_token != claim_token:
            return None
        if record.state == ManagedThreadSideEffectState.CLAIMED:
            self._ledger.patch(effect_id, state=ManagedThreadSideEffectState.RUNNING)
        if result.outcome == ManagedThreadSideEffectOutcome.SUCCEEDED:
            return self._ledger.patch(
                effect_id,
                state=ManagedThreadSideEffectState.SUCCEEDED,
                completed_at=now_iso(),
                claim_token=None,
                last_error=None,
            )
        if result.outcome == ManagedThreadSideEffectOutcome.ABANDONED:
            return self._ledger.patch(
                effect_id,
                state=ManagedThreadSideEffectState.ABANDONED,
                claim_token=None,
                last_error=result.error or "abandoned",
            )
        if (
            result.outcome == ManagedThreadSideEffectOutcome.FAILED
            and record.attempt_count >= self._max_attempts
        ):
            return self._ledger.patch(
                effect_id,
                state=ManagedThreadSideEffectState.FAILED,
                claim_token=None,
                last_error=result.error or "max_attempts_exceeded",
            )
        return self._ledger.patch(
            effect_id,
            state=ManagedThreadSideEffectState.RETRY_SCHEDULED,
            claim_token=None,
            next_attempt_at=_compute_next_attempt_at(
                record.attempt_count, self._retry_backoff
            ),
            last_error=result.error,
        )

    def recover_expired_claims(self, *, now: Optional[datetime] = None) -> int:
        current_at = now or datetime.now(timezone.utc)
        recovered = 0
        with open_orchestration_sqlite(
            self._ledger._hub_root, durable=self._ledger._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_managed_thread_side_effects
                 WHERE state IN ('claimed', 'running')
                   AND claim_expires_at IS NOT NULL
                   AND claim_expires_at <= ?
                """,
                (current_at.isoformat(),),
            ).fetchall()
        for row in rows:
            record = _record_from_row(row)
            self._ledger.patch(
                record.effect_id,
                state=ManagedThreadSideEffectState.RETRY_SCHEDULED,
                claim_token=None,
                next_attempt_at=current_at.isoformat(),
                last_error="claim_expired",
            )
            recovered += 1
        return recovered


def _compute_next_attempt_at(attempt_count: int, backoff: timedelta) -> str:
    exponent = max(0, int(attempt_count or 0) - 1)
    delay = min(
        backoff.total_seconds() * (2**exponent), _DEFAULT_MAX_BACKOFF.total_seconds()
    )
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay))).isoformat()


def _record_to_row_values(record: ManagedThreadSideEffectRecord) -> tuple[Any, ...]:
    return (
        record.effect_id,
        record.managed_thread_id,
        record.managed_turn_id,
        record.idempotency_key,
        record.effect_kind,
        record.surface_kind,
        record.surface_key,
        json.dumps(dict(record.payload or {}), sort_keys=True),
        record.state.value,
        max(0, int(record.attempt_count or 0)),
        record.claim_token,
        record.claimed_at,
        record.claim_expires_at,
        record.next_attempt_at,
        record.completed_at,
        record.last_error,
        record.created_at or now_iso(),
        record.updated_at or now_iso(),
    )


def _record_from_row(row: Any) -> ManagedThreadSideEffectRecord:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return ManagedThreadSideEffectRecord(
        effect_id=str(row["effect_id"]),
        managed_thread_id=str(row["managed_thread_id"]),
        managed_turn_id=str(row["managed_turn_id"]),
        idempotency_key=str(row["idempotency_key"]),
        effect_kind=str(row["effect_kind"]),
        surface_kind=str(row["surface_kind"]),
        surface_key=str(row["surface_key"]),
        payload=payload if isinstance(payload, Mapping) else {},
        state=ManagedThreadSideEffectState(str(row["state"])),
        attempt_count=int(row["attempt_count"] or 0),
        claim_token=row["claim_token"],
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        next_attempt_at=row["next_attempt_at"],
        completed_at=row["completed_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
