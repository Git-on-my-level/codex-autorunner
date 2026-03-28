from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..time_utils import now_iso
from .sqlite import open_orchestration_sqlite


def _normalize_optional_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class RuntimeThreadBinding:
    backend_thread_id: Optional[str]
    backend_runtime_instance_id: Optional[str] = None


def _ensure_runtime_bindings_table(hub_root: Path) -> None:
    with open_orchestration_sqlite(hub_root) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orch_runtime_thread_bindings (
                    thread_target_id TEXT PRIMARY KEY,
                    backend_thread_id TEXT NOT NULL,
                    backend_runtime_instance_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )


def _normalized_thread_target_id(thread_target_id: str) -> Optional[str]:
    return _normalize_optional_text(thread_target_id)


def get_runtime_thread_binding(
    hub_root: Path, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    normalized_thread_target_id = _normalized_thread_target_id(thread_target_id)
    if normalized_thread_target_id is None:
        return None
    _ensure_runtime_bindings_table(hub_root)
    with open_orchestration_sqlite(hub_root) as conn:
        row = conn.execute(
            """
            SELECT backend_thread_id, backend_runtime_instance_id
              FROM orch_runtime_thread_bindings
             WHERE thread_target_id = ?
            """,
            (normalized_thread_target_id,),
        ).fetchone()
    if row is None:
        return None
    return RuntimeThreadBinding(
        backend_thread_id=_normalize_optional_text(row["backend_thread_id"]),
        backend_runtime_instance_id=_normalize_optional_text(
            row["backend_runtime_instance_id"]
        ),
    )


def set_runtime_thread_binding(
    hub_root: Path,
    thread_target_id: str,
    *,
    backend_thread_id: Optional[str],
    backend_runtime_instance_id: Optional[str] = None,
) -> None:
    normalized_thread_target_id = _normalized_thread_target_id(thread_target_id)
    if normalized_thread_target_id is None:
        return
    normalized_backend_thread_id = _normalize_optional_text(backend_thread_id)
    normalized_runtime_instance_id = _normalize_optional_text(
        backend_runtime_instance_id
    )
    _ensure_runtime_bindings_table(hub_root)
    with open_orchestration_sqlite(hub_root) as conn:
        with conn:
            if normalized_backend_thread_id is None:
                conn.execute(
                    """
                    DELETE FROM orch_runtime_thread_bindings
                     WHERE thread_target_id = ?
                    """,
                    (normalized_thread_target_id,),
                )
                return
            conn.execute(
                """
                INSERT INTO orch_runtime_thread_bindings (
                    thread_target_id,
                    backend_thread_id,
                    backend_runtime_instance_id,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_target_id) DO UPDATE SET
                    backend_thread_id = excluded.backend_thread_id,
                    backend_runtime_instance_id = excluded.backend_runtime_instance_id,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_thread_target_id,
                    normalized_backend_thread_id,
                    normalized_runtime_instance_id,
                    now_iso(),
                ),
            )


def clear_runtime_thread_binding(hub_root: Path, thread_target_id: str) -> None:
    set_runtime_thread_binding(
        hub_root,
        thread_target_id,
        backend_thread_id=None,
        backend_runtime_instance_id=None,
    )


__all__ = [
    "RuntimeThreadBinding",
    "clear_runtime_thread_binding",
    "get_runtime_thread_binding",
    "set_runtime_thread_binding",
]
