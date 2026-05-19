from __future__ import annotations

import sqlite3
from typing import Any

from ..context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleLedgerKey,
    ContextCapsuleLedgerObservation,
    ContextCapsuleRenderPlan,
    ContextCapsuleVisibility,
    plan_capsule_render,
)
from ..text_utils import _json_dumps, _json_loads_object
from ..time_utils import now_iso


class SQLiteContextCapsuleLedger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_observation(
        self, key: ContextCapsuleLedgerKey
    ) -> ContextCapsuleLedgerObservation | None:
        row = self._conn.execute(
            """
            SELECT *
              FROM orch_context_capsule_ledger
             WHERE observation_id = ?
            """,
            (key.stable_id(),),
        ).fetchone()
        if row is None:
            return None
        return _row_to_observation(row)

    def plan_render(
        self,
        capsule: ContextCapsule,
        *,
        surface_kind: str,
        surface_key: str,
        managed_thread_id: str,
        backend_thread_id: str | None,
        scope_id: str,
        force_refresh: bool = False,
    ) -> ContextCapsuleRenderPlan:
        key = ContextCapsuleLedgerKey.from_capsule(
            capsule,
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            backend_thread_id=backend_thread_id,
            scope_id=scope_id,
        )
        return plan_capsule_render(
            capsule,
            key,
            previous=self.get_observation(key),
            force_refresh=force_refresh,
        )

    def record_render(self, plan: ContextCapsuleRenderPlan) -> None:
        capsule = plan.capsule
        key = plan.key
        observed_at = now_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO orch_context_capsule_ledger (
                    observation_id,
                    surface_kind,
                    surface_key,
                    managed_thread_id,
                    backend_thread_id,
                    scope_kind,
                    scope_id,
                    capsule_id,
                    capsule_version,
                    visibility,
                    expiry,
                    source_digest,
                    payload_digest,
                    render_reason,
                    payload_json,
                    first_observed_at,
                    last_observed_at,
                    render_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(observation_id) DO UPDATE SET
                    visibility = excluded.visibility,
                    expiry = excluded.expiry,
                    source_digest = excluded.source_digest,
                    payload_digest = excluded.payload_digest,
                    render_reason = excluded.render_reason,
                    payload_json = excluded.payload_json,
                    last_observed_at = excluded.last_observed_at,
                    render_count = orch_context_capsule_ledger.render_count + 1
                """,
                (
                    key.stable_id(),
                    key.surface_kind,
                    key.surface_key,
                    key.managed_thread_id,
                    key.backend_thread_id,
                    key.scope_kind,
                    key.scope_id,
                    key.capsule_id,
                    key.capsule_version,
                    capsule.visibility.value,
                    capsule.expiry.value,
                    capsule.source_digest,
                    plan.payload_digest,
                    capsule.reason,
                    _json_dumps(capsule.canonical_payload()),
                    observed_at,
                    observed_at,
                ),
            )


def _row_to_observation(row: Any) -> ContextCapsuleLedgerObservation:
    payload = _json_loads_object(row["payload_json"])
    visibility = ContextCapsuleVisibility(str(row["visibility"]))
    expiry = ContextCapsuleExpiry(str(row["expiry"]))
    return ContextCapsuleLedgerObservation(
        key=ContextCapsuleLedgerKey(
            surface_kind=str(row["surface_kind"] or ""),
            surface_key=str(row["surface_key"] or ""),
            managed_thread_id=str(row["managed_thread_id"] or ""),
            backend_thread_id=str(row["backend_thread_id"] or ""),
            scope_kind=str(row["scope_kind"] or ""),
            scope_id=str(row["scope_id"] or ""),
            capsule_id=str(row["capsule_id"] or ""),
            capsule_version=str(row["capsule_version"] or ""),
        ),
        payload_digest=str(row["payload_digest"] or ""),
        source_digest=str(row["source_digest"] or payload.get("source_digest") or ""),
        expiry=expiry,
        visibility=visibility,
        reason=str(row["render_reason"] or ""),
    )


__all__ = ["SQLiteContextCapsuleLedger"]
