from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ..logging_utils import log_event
from ..text_utils import _normalize_optional_text
from ..time_utils import now_iso
from .sqlite import open_orchestration_sqlite

BACKEND_BINDING_BOUND = "bound"
BACKEND_BINDING_SUSPECT = "suspect"
BACKEND_BINDING_INVALID = "invalid"
BACKEND_BINDING_FRESH_REQUIRED = "fresh_required"
BACKEND_BINDING_STATES = frozenset(
    {
        BACKEND_BINDING_BOUND,
        BACKEND_BINDING_SUSPECT,
        BACKEND_BINDING_INVALID,
        BACKEND_BINDING_FRESH_REQUIRED,
    }
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeThreadBinding:
    backend_thread_id: Optional[str]
    backend_runtime_instance_id: Optional[str] = None
    binding_state: str = BACKEND_BINDING_BOUND
    state_reason: Optional[str] = None


def normalize_backend_binding_state(value: object) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return BACKEND_BINDING_BOUND
    normalized = normalized.lower()
    if normalized in BACKEND_BINDING_STATES:
        return normalized
    raise ValueError(f"Invalid backend binding state: {normalized}")


def _ensure_runtime_bindings_table(hub_root: Path) -> None:
    with open_orchestration_sqlite(hub_root) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orch_runtime_thread_bindings (
                    thread_target_id TEXT PRIMARY KEY,
                    backend_thread_id TEXT NOT NULL,
                    backend_runtime_instance_id TEXT,
                    binding_state TEXT NOT NULL DEFAULT 'bound',
                    state_reason TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(orch_runtime_thread_bindings)"
                ).fetchall()
            }
            if "binding_state" not in columns:
                conn.execute("""
                    ALTER TABLE orch_runtime_thread_bindings
                    ADD COLUMN binding_state TEXT NOT NULL DEFAULT 'bound'
                    """)
            if "state_reason" not in columns:
                conn.execute("""
                    ALTER TABLE orch_runtime_thread_bindings
                    ADD COLUMN state_reason TEXT
                    """)


def _normalized_thread_target_id(thread_target_id: str) -> Optional[str]:
    return _normalize_optional_text(thread_target_id)


def _binding_state(binding: Optional[RuntimeThreadBinding]) -> Optional[str]:
    if binding is None:
        return None
    return normalize_backend_binding_state(binding.binding_state)


def _log_binding_transition(
    *,
    thread_target_id: str,
    previous_binding: Optional[RuntimeThreadBinding],
    next_binding: Optional[RuntimeThreadBinding],
    reason: Optional[str],
    transition: str,
) -> None:
    log_event(
        logger,
        logging.INFO,
        "orchestration.thread.binding_transition",
        thread_target_id=thread_target_id,
        transition=transition,
        previous_backend_thread_id=(
            previous_binding.backend_thread_id if previous_binding is not None else None
        ),
        previous_runtime_instance_id=(
            previous_binding.backend_runtime_instance_id
            if previous_binding is not None
            else None
        ),
        previous_state=_binding_state(previous_binding),
        next_backend_thread_id=(
            next_binding.backend_thread_id if next_binding is not None else None
        ),
        next_runtime_instance_id=(
            next_binding.backend_runtime_instance_id
            if next_binding is not None
            else None
        ),
        next_state=_binding_state(next_binding),
        reason=reason,
    )


class BackendConversationBindingService:
    """Domain helper for runtime backend conversation binding transitions."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root

    @staticmethod
    def allows_resume(binding: Optional[RuntimeThreadBinding]) -> bool:
        if binding is None or not binding.backend_thread_id:
            return False
        state = normalize_backend_binding_state(binding.binding_state)
        return state not in {
            BACKEND_BINDING_INVALID,
            BACKEND_BINDING_FRESH_REQUIRED,
        }

    @staticmethod
    def normalize_state(value: object) -> str:
        return normalize_backend_binding_state(value)

    @staticmethod
    def mark_thread_store_state(
        thread_store: Any,
        thread_target_id: str,
        *,
        binding_state: str,
        state_reason: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        normalized_state = normalize_backend_binding_state(binding_state)
        normalized_reason = _normalize_optional_text(state_reason)
        marker = getattr(thread_store, "mark_thread_runtime_binding_state", None)
        if callable(marker):
            marked = marker(
                thread_target_id,
                binding_state=normalized_state,
                state_reason=normalized_reason,
            )
            if marked is not None:
                return cast(RuntimeThreadBinding, marked)
        normalized_backend_thread_id = _normalize_optional_text(backend_thread_id)
        if normalized_backend_thread_id is None:
            return None
        setter = getattr(thread_store, "set_thread_backend_binding", None)
        if not callable(setter):
            return None
        setter(
            thread_target_id,
            normalized_backend_thread_id,
            binding_state=normalized_state,
            state_reason=normalized_reason,
        )
        getter = getattr(thread_store, "get_thread_runtime_binding", None)
        if callable(getter):
            binding = getter(thread_target_id)
            if isinstance(binding, RuntimeThreadBinding):
                return binding
        return None

    def get(self, thread_target_id: str) -> Optional[RuntimeThreadBinding]:
        return _get_runtime_thread_binding(self._hub_root, thread_target_id)

    def set(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str],
        backend_runtime_instance_id: Optional[str] = None,
        binding_state: str = BACKEND_BINDING_BOUND,
        state_reason: Optional[str] = None,
    ) -> None:
        normalized_thread_target_id = _normalized_thread_target_id(thread_target_id)
        if normalized_thread_target_id is None:
            return
        current = self.get(normalized_thread_target_id)
        normalized_backend_thread_id = _normalize_optional_text(backend_thread_id)
        normalized_runtime_instance_id = _normalize_optional_text(
            backend_runtime_instance_id
        )
        normalized_binding_state = normalize_backend_binding_state(binding_state)
        normalized_state_reason = _normalize_optional_text(state_reason)
        _ensure_runtime_bindings_table(self._hub_root)
        with open_orchestration_sqlite(self._hub_root) as conn:
            with conn:
                if normalized_backend_thread_id is None:
                    conn.execute(
                        """
                        DELETE FROM orch_runtime_thread_bindings
                         WHERE thread_target_id = ?
                        """,
                        (normalized_thread_target_id,),
                    )
                    if current is not None:
                        _log_binding_transition(
                            thread_target_id=normalized_thread_target_id,
                            previous_binding=current,
                            next_binding=None,
                            reason=normalized_state_reason,
                            transition="clear",
                        )
                    return
                conn.execute(
                    """
                    INSERT INTO orch_runtime_thread_bindings (
                        thread_target_id,
                        backend_thread_id,
                        backend_runtime_instance_id,
                        binding_state,
                        state_reason,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_target_id) DO UPDATE SET
                        backend_thread_id = excluded.backend_thread_id,
                        backend_runtime_instance_id = excluded.backend_runtime_instance_id,
                        binding_state = excluded.binding_state,
                        state_reason = excluded.state_reason,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_thread_target_id,
                        normalized_backend_thread_id,
                        normalized_runtime_instance_id,
                        normalized_binding_state,
                        normalized_state_reason,
                        now_iso(),
                    ),
                )
        updated = self.get(normalized_thread_target_id)
        _log_binding_transition(
            thread_target_id=normalized_thread_target_id,
            previous_binding=current,
            next_binding=updated,
            reason=normalized_state_reason,
            transition="set",
        )

    def mark_state(
        self,
        thread_target_id: str,
        *,
        binding_state: str,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        normalized_thread_target_id = _normalized_thread_target_id(thread_target_id)
        if normalized_thread_target_id is None:
            return None
        current = self.get(normalized_thread_target_id)
        if current is None or current.backend_thread_id is None:
            return current
        self.set(
            normalized_thread_target_id,
            backend_thread_id=current.backend_thread_id,
            backend_runtime_instance_id=current.backend_runtime_instance_id,
            binding_state=binding_state,
            state_reason=state_reason,
        )
        return self.get(normalized_thread_target_id)

    def mark_bound(
        self,
        thread_target_id: str,
        *,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return self.mark_state(
            thread_target_id,
            binding_state=BACKEND_BINDING_BOUND,
            state_reason=state_reason,
        )

    def mark_suspect(
        self,
        thread_target_id: str,
        *,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return self.mark_state(
            thread_target_id,
            binding_state=BACKEND_BINDING_SUSPECT,
            state_reason=state_reason,
        )

    def mark_invalid(
        self,
        thread_target_id: str,
        *,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return self.mark_state(
            thread_target_id,
            binding_state=BACKEND_BINDING_INVALID,
            state_reason=state_reason,
        )

    def mark_fresh_required(
        self,
        thread_target_id: str,
        *,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        return self.mark_state(
            thread_target_id,
            binding_state=BACKEND_BINDING_FRESH_REQUIRED,
            state_reason=state_reason,
        )

    def clear(self, thread_target_id: str) -> None:
        self.set(
            thread_target_id,
            backend_thread_id=None,
            backend_runtime_instance_id=None,
        )


def _get_runtime_thread_binding(
    hub_root: Path, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    normalized_thread_target_id = _normalized_thread_target_id(thread_target_id)
    if normalized_thread_target_id is None:
        return None
    _ensure_runtime_bindings_table(hub_root)
    with open_orchestration_sqlite(hub_root) as conn:
        row = conn.execute(
            """
            SELECT backend_thread_id, backend_runtime_instance_id,
                   binding_state, state_reason
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
        binding_state=normalize_backend_binding_state(row["binding_state"]),
        state_reason=_normalize_optional_text(row["state_reason"]),
    )


def get_runtime_thread_binding(
    hub_root: Path, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    return BackendConversationBindingService(hub_root).get(thread_target_id)


def set_runtime_thread_binding(
    hub_root: Path,
    thread_target_id: str,
    *,
    backend_thread_id: Optional[str],
    backend_runtime_instance_id: Optional[str] = None,
    binding_state: str = BACKEND_BINDING_BOUND,
    state_reason: Optional[str] = None,
) -> None:
    BackendConversationBindingService(hub_root).set(
        thread_target_id,
        backend_thread_id=backend_thread_id,
        backend_runtime_instance_id=backend_runtime_instance_id,
        binding_state=binding_state,
        state_reason=state_reason,
    )


def mark_runtime_thread_binding_state(
    hub_root: Path,
    thread_target_id: str,
    *,
    binding_state: str,
    state_reason: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return BackendConversationBindingService(hub_root).mark_state(
        thread_target_id,
        binding_state=binding_state,
        state_reason=state_reason,
    )


def clear_runtime_thread_binding(hub_root: Path, thread_target_id: str) -> None:
    BackendConversationBindingService(hub_root).clear(thread_target_id)


def runtime_thread_binding_allows_resume(
    binding: Optional[RuntimeThreadBinding],
) -> bool:
    return BackendConversationBindingService.allows_resume(binding)


def mark_thread_store_runtime_binding_state(
    thread_store: Any,
    thread_target_id: str,
    *,
    binding_state: str,
    state_reason: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return BackendConversationBindingService.mark_thread_store_state(
        thread_store,
        thread_target_id,
        binding_state=binding_state,
        state_reason=state_reason,
        backend_thread_id=backend_thread_id,
    )


def mark_thread_store_runtime_binding_bound(
    thread_store: Any,
    thread_target_id: str,
    *,
    state_reason: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return mark_thread_store_runtime_binding_state(
        thread_store,
        thread_target_id,
        binding_state=BACKEND_BINDING_BOUND,
        state_reason=state_reason,
        backend_thread_id=backend_thread_id,
    )


def mark_thread_store_runtime_binding_suspect(
    thread_store: Any,
    thread_target_id: str,
    *,
    state_reason: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return mark_thread_store_runtime_binding_state(
        thread_store,
        thread_target_id,
        binding_state=BACKEND_BINDING_SUSPECT,
        state_reason=state_reason,
        backend_thread_id=backend_thread_id,
    )


def mark_thread_store_runtime_binding_invalid(
    thread_store: Any,
    thread_target_id: str,
    *,
    state_reason: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return mark_thread_store_runtime_binding_state(
        thread_store,
        thread_target_id,
        binding_state=BACKEND_BINDING_INVALID,
        state_reason=state_reason,
        backend_thread_id=backend_thread_id,
    )


def mark_thread_store_runtime_binding_fresh_required(
    thread_store: Any,
    thread_target_id: str,
    *,
    state_reason: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
) -> Optional[RuntimeThreadBinding]:
    return mark_thread_store_runtime_binding_state(
        thread_store,
        thread_target_id,
        binding_state=BACKEND_BINDING_FRESH_REQUIRED,
        state_reason=state_reason,
        backend_thread_id=backend_thread_id,
    )


__all__ = [
    "BACKEND_BINDING_BOUND",
    "BACKEND_BINDING_FRESH_REQUIRED",
    "BACKEND_BINDING_INVALID",
    "BACKEND_BINDING_STATES",
    "BACKEND_BINDING_SUSPECT",
    "BackendConversationBindingService",
    "RuntimeThreadBinding",
    "clear_runtime_thread_binding",
    "get_runtime_thread_binding",
    "mark_runtime_thread_binding_state",
    "mark_thread_store_runtime_binding_bound",
    "mark_thread_store_runtime_binding_fresh_required",
    "mark_thread_store_runtime_binding_invalid",
    "mark_thread_store_runtime_binding_suspect",
    "mark_thread_store_runtime_binding_state",
    "normalize_backend_binding_state",
    "runtime_thread_binding_allows_resume",
    "set_runtime_thread_binding",
]
