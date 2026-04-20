from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
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
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_REACTIVE_STATE_FILENAME
        )

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is not None:
                return state
            state = self._load_legacy_file_unlocked()
            if state is not None:
                return state
            state = default_pma_reactive_state()
            self._save_unlocked(state)
            return state

    def check_and_update(self, key: str, debounce_seconds: int) -> bool:
        """
        Return True if enqueue is allowed; otherwise False if debounced.
        Updates the last_enqueued timestamp when allowed.
        """
        now = time.time()
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is None:
                state = self._load_legacy_file_unlocked()
            if state is None:
                state = default_pma_reactive_state()
            last_enqueued = state.get("last_enqueued")
            if not isinstance(last_enqueued, dict):
                last_enqueued = {}
                state["last_enqueued"] = last_enqueued

            last = last_enqueued.get(key)
            if debounce_seconds > 0 and isinstance(last, (int, float)):
                if now - float(last) < debounce_seconds:
                    return False

            last_enqueued[key] = now
            self._save_unlocked(state)
        return True

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        try:
            with open_orchestration_sqlite(
                self._hub_root,
                durable=True,
                migrate=False,
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT debounce_key, last_enqueued_at
                      FROM orch_reactive_debounce_state
                     ORDER BY debounce_key ASC
                    """
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return None
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

    def _load_legacy_file_unlocked(self) -> Optional[dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return default_pma_reactive_state()
        return parsed if isinstance(parsed, dict) else default_pma_reactive_state()

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        last_enqueued = state.get("last_enqueued")
        values = last_enqueued if isinstance(last_enqueued, dict) else {}
        stamp = now_iso()
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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps(state, indent=2) + "\n")
        except OSError as exc:
            logger.warning(
                "Failed to write reactive state mirror at %s: %s", self._path, exc
            )


__all__ = [
    "PMA_REACTIVE_STATE_FILENAME",
    "PmaReactiveStore",
    "default_pma_reactive_state",
]
