from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_autorunner.core.context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleRenderDecision,
    ContextCapsuleScope,
    ContextCapsuleVisibility,
)
from codex_autorunner.core.orchestration import (
    SQLiteContextCapsuleLedger,
    apply_orchestration_migrations,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _capsule(source_digest: str = "source-1") -> ContextCapsule:
    return ContextCapsule(
        capsule_id="car.ticket_flow",
        version=1,
        scope=ContextCapsuleScope.BACKEND_SESSION,
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest=source_digest,
        expiry=ContextCapsuleExpiry.WHEN_SOURCE_CHANGES,
        reason="ticket_flow",
        payload={"text": "ticket guidance"},
    )


def test_sqlite_context_capsule_ledger_dedupes_by_canonical_key(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"
    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        ledger = SQLiteContextCapsuleLedger(conn)
        first = ledger.plan_render(
            _capsule(),
            surface_kind="discord",
            surface_key="channel/thread",
            managed_thread_id="managed-1",
            backend_thread_id="backend-1",
            scope_id="backend-1",
        )
        ledger.record_render(first)

        duplicate = ledger.plan_render(
            _capsule(),
            surface_kind="discord",
            surface_key="channel/thread",
            managed_thread_id="managed-1",
            backend_thread_id="backend-1",
            scope_id="backend-1",
        )

        assert first.decision is ContextCapsuleRenderDecision.NEW
        assert duplicate.decision is ContextCapsuleRenderDecision.SKIP_DUPLICATE
        assert duplicate.should_render is False


def test_sqlite_context_capsule_ledger_distinguishes_backend_sessions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"
    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        ledger = SQLiteContextCapsuleLedger(conn)
        first = ledger.plan_render(
            _capsule(),
            surface_kind="discord",
            surface_key="channel/thread",
            managed_thread_id="managed-1",
            backend_thread_id="backend-1",
            scope_id="backend-1",
        )
        ledger.record_render(first)
        next_backend = ledger.plan_render(
            _capsule(),
            surface_kind="discord",
            surface_key="channel/thread",
            managed_thread_id="managed-1",
            backend_thread_id="backend-2",
            scope_id="backend-2",
        )

        assert next_backend.decision is ContextCapsuleRenderDecision.NEW


def test_sqlite_context_capsule_ledger_detects_payload_change(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "orchestration.sqlite3"
    with _connect(db_path) as conn:
        apply_orchestration_migrations(conn)
        ledger = SQLiteContextCapsuleLedger(conn)
        first = ledger.plan_render(
            _capsule(),
            surface_kind="pma",
            surface_key="managed-1",
            managed_thread_id="managed-1",
            backend_thread_id="backend-1",
            scope_id="backend-1",
        )
        ledger.record_render(first)
        changed = ledger.plan_render(
            _capsule("source-2"),
            surface_kind="pma",
            surface_key="managed-1",
            managed_thread_id="managed-1",
            backend_thread_id="backend-1",
            scope_id="backend-1",
        )

        assert changed.decision is ContextCapsuleRenderDecision.CHANGED
