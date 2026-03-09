from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .pma_automation_types import (
    PMA_AUTOMATION_STORE_FILENAME,
    PMA_AUTOMATION_VERSION,
    _normalize_text,
    default_pma_automation_state,
)
from .utils import atomic_write

logger = logging.getLogger(__name__)


class PmaAutomationPersistence:
    def __init__(self, hub_root: Path) -> None:
        self._path = (
            hub_root / ".codex-autorunner" / "pma" / PMA_AUTOMATION_STORE_FILENAME
        )

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is None:
                state = default_pma_automation_state()
                self._save_unlocked(state)
            return state

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to read PMA automation store at %s: %s", self._path, exc
            )
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return default_pma_automation_state()
        if not isinstance(parsed, dict):
            return default_pma_automation_state()
        return self._normalize_loaded_state(parsed)

    def _normalize_loaded_state(self, parsed: dict[str, Any]) -> dict[str, Any]:
        state = default_pma_automation_state()
        state["version"] = int(parsed.get("version", PMA_AUTOMATION_VERSION) or 1)
        state["updated_at"] = (
            _normalize_text(parsed.get("updated_at")) or state["updated_at"]
        )
        state["subscriptions"] = parsed.get("subscriptions", [])
        state["timers"] = parsed.get("timers", [])
        state["wakeups"] = parsed.get("wakeups", [])
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(state, indent=2) + "\n")

    def save(self, state: dict[str, Any]) -> None:
        with file_lock(self._lock_path()):
            self._save_unlocked(state)


__all__ = ["PmaAutomationPersistence"]
