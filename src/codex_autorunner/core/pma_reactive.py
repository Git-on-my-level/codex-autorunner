from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
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
        self._path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_REACTIVE_STATE_FILENAME
        )

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
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
            self._save_unlocked(state)
        return True

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to read PMA reactive state at %s: %s", self._path, exc
            )
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return default_pma_reactive_state()
        if not isinstance(data, dict):
            return default_pma_reactive_state()
        return data

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(state, indent=2) + "\n")


__all__ = [
    "PMA_REACTIVE_STATE_FILENAME",
    "PmaReactiveStore",
    "default_pma_reactive_state",
]
