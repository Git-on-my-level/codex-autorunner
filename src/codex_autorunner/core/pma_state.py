from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .time_utils import now_iso
from .utils import atomic_write

PMA_STATE_FILENAME = "state.json"
PMA_STATE_CORRUPT_SUFFIX = ".corrupt"
PMA_STATE_NOTICE_SUFFIX = ".corrupt.json"

logger = logging.getLogger(__name__)


def default_pma_state() -> dict[str, Any]:
    return {
        "version": 1,
        "active": False,
        "current": {},
        "last_result": {},
        "updated_at": now_iso(),
    }


def default_pma_state_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_STATE_FILENAME


class PmaStateStore:
    def __init__(self, hub_root: Path) -> None:
        self._path = default_pma_state_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _notice_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}{PMA_STATE_NOTICE_SUFFIX}")

    def load(self, *, ensure_exists: bool = True) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            state = self._load_unlocked()
            if state is not None:
                return state
            state = default_pma_state()
            if ensure_exists:
                self._save_unlocked(state)
            return state

    def save(self, state: dict[str, Any]) -> None:
        with file_lock(self._lock_path()):
            self._save_unlocked(state)

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read PMA state at %s: %s", self._path, exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._handle_corrupt_state(str(exc))
            return default_pma_state()
        if not isinstance(data, dict):
            self._handle_corrupt_state(
                f"Expected JSON object, got {type(data).__name__}"
            )
            return default_pma_state()
        return data

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(state, indent=2) + "\n")

    def _handle_corrupt_state(self, detail: str) -> None:
        stamp = self._stamp()
        backup_path = self._path.with_name(
            f"{self._path.name}{PMA_STATE_CORRUPT_SUFFIX}.{stamp}"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.replace(backup_path)
            backup_value = str(backup_path)
        except OSError:
            backup_value = ""
        notice = {
            "status": "corrupt",
            "message": "PMA state reset due to corrupted state.json.",
            "detail": detail,
            "detected_at": stamp,
            "backup_path": backup_value,
        }
        notice_path = self._notice_path()
        try:
            atomic_write(notice_path, json.dumps(notice, indent=2) + "\n")
        except Exception:
            logger.warning("Failed to write PMA corruption notice at %s", notice_path)
        try:
            self._save_unlocked(default_pma_state())
        except Exception:
            logger.warning("Failed to reset PMA state at %s", self._path)
