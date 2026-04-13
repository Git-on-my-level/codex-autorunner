"""Reactive debounce store for PMA lifecycle event gating.

Canonical ownership:
  ``orch_reactive_debounce_state`` in the orchestration SQLite database is the
  sole authoritative owner of debounce state.  Every read goes through SQLite,
  and every write persists to SQLite first.

Compatibility mirror:
  After each canonical write the store also writes a JSON mirror to
  ``.codex-autorunner/pma/reactive_state.json``.  This mirror exists solely for
  backward compatibility and operator visibility; it must never be treated as a
  source of truth.  Deleting the mirror does not affect correctness.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .orchestration.legacy_backfill_gate import ensure_legacy_orchestration_backfill
from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import lock_path_for
from .time_utils import now_iso
from .utils import atomic_write

PMA_REACTIVE_STATE_FILENAME = "reactive_state.json"

logger = logging.getLogger(__name__)


def default_pma_reactive_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_enqueued": {},
    }


class PmaReactiveStore:
    """Debounce gate for PMA reactive lifecycle events.

    Reads always come from the canonical ``orch_reactive_debounce_state``
    table.  Writes persist to that table first, then emit a best-effort JSON
    mirror for compatibility.
    """

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._compat_mirror_path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_REACTIVE_STATE_FILENAME
        )

    def _lock_path(self) -> Path:
        return lock_path_for(self._compat_mirror_path)

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is not None:
                return state
            state = default_pma_reactive_state()
            self._persist_canonical_unlocked(state)
            self._write_compat_mirror(state)
            return state

    def check_and_update(self, key: str, debounce_seconds: int) -> bool:
        """
        Return True if enqueue is allowed; otherwise False if debounced.
        Updates the last_enqueued timestamp when allowed.
        """
        now = time.time()
        with file_lock(self._lock_path()):
            state = self._load_unlocked() or default_pma_reactive_state()
            last_enqueued = state.get("last_enqueued")
            if not isinstance(last_enqueued, dict):
                last_enqueued = {}
                state["last_enqueued"] = last_enqueued

            last = last_enqueued.get(key)
            if debounce_seconds > 0 and isinstance(last, (int, float)):
                if now - float(last) < debounce_seconds:
                    return False

            last_enqueued[key] = now
            self._persist_canonical_unlocked(state)
            self._write_compat_mirror(state)
        return True

    # ------------------------------------------------------------------
    # Canonical persistence (orchestration SQLite)
    # ------------------------------------------------------------------

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        ensure_legacy_orchestration_backfill(self._hub_root, durable=True)
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT debounce_key, last_enqueued_at
                  FROM orch_reactive_debounce_state
                 ORDER BY debounce_key ASC
                """
            ).fetchall()
        if not rows:
            return None
        return {
            "version": 1,
            "last_enqueued": {
                str(row["debounce_key"]): float(row["last_enqueued_at"])
                for row in rows
                if row["debounce_key"] is not None
                and row["last_enqueued_at"] is not None
            },
        }

    def _persist_canonical_unlocked(self, state: dict[str, Any]) -> None:
        last_enqueued = state.get("last_enqueued")
        values = last_enqueued if isinstance(last_enqueued, dict) else {}
        stamp = now_iso()
        ensure_legacy_orchestration_backfill(self._hub_root, durable=True)
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            with conn:
                conn.execute("DELETE FROM orch_reactive_debounce_state")
                for key, raw_value in values.items():
                    if not isinstance(key, str):
                        continue
                    try:
                        parsed = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                    conn.execute(
                        """
                        INSERT INTO orch_reactive_debounce_state (
                            debounce_key,
                            repo_id,
                            thread_target_id,
                            fingerprint,
                            available_at,
                            last_event_id,
                            metadata_json,
                            created_at,
                            updated_at,
                            last_enqueued_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (key, None, None, None, None, None, "{}", stamp, stamp, parsed),
                    )

    # ------------------------------------------------------------------
    # Compatibility mirror (JSON file — not a source of truth)
    # ------------------------------------------------------------------

    def _write_compat_mirror(self, state: dict[str, Any]) -> None:
        try:
            self._compat_mirror_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._compat_mirror_path, json.dumps(state, indent=2) + "\n")
        except OSError:
            logger.debug(
                "Failed to write reactive debounce compat mirror at %s",
                self._compat_mirror_path,
                exc_info=True,
            )


__all__ = [
    "PMA_REACTIVE_STATE_FILENAME",
    "PmaReactiveStore",
    "default_pma_reactive_state",
]
