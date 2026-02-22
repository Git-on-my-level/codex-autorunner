from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .time_utils import now_iso
from .utils import atomic_write

PMA_ACTIVE_SINK_FILENAME = "active_sink.json"

logger = logging.getLogger(__name__)


class PmaActiveSinkStore:
    def __init__(self, hub_root: Path) -> None:
        self._path = hub_root / ".codex-autorunner" / "pma" / PMA_ACTIVE_SINK_FILENAME

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def load(self) -> Optional[dict[str, Any]]:
        with file_lock(self._lock_path()):
            return self._load_unlocked()

    def set_web(self) -> dict[str, Any]:
        payload = {
            "version": 1,
            "kind": "web",
            "updated_at": now_iso(),
            "last_delivery_turn_id": None,
        }
        with file_lock(self._lock_path()):
            self._save_unlocked(payload)
        return payload

    def set_telegram(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        topic_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "kind": "telegram",
            "chat_id": int(chat_id),
            "thread_id": int(thread_id) if thread_id is not None else None,
            "updated_at": now_iso(),
            "last_delivery_turn_id": None,
        }
        if topic_key:
            payload["topic_key"] = topic_key
        with file_lock(self._lock_path()):
            self._save_unlocked(payload)
        return payload

    def set_chat(
        self,
        platform: str,
        *,
        chat_id: str,
        thread_id: Optional[str] = None,
        conversation_key: Optional[str] = None,
    ) -> dict[str, Any]:
        platform_norm = str(platform or "").strip().lower()
        chat_id_norm = str(chat_id or "").strip()
        if not platform_norm:
            raise ValueError("platform must be non-empty")
        if not chat_id_norm:
            raise ValueError("chat_id must be non-empty")
        thread_id_norm = (
            str(thread_id).strip() if isinstance(thread_id, str) and thread_id else None
        )
        conversation_key_norm = (
            str(conversation_key).strip()
            if isinstance(conversation_key, str) and conversation_key
            else None
        )
        with file_lock(self._lock_path()):
            existing = self._load_unlocked()
            last_delivery_turn_id = (
                existing.get("last_delivery_turn_id")
                if isinstance(existing, dict)
                and isinstance(existing.get("last_delivery_turn_id"), str)
                else None
            )
            payload: dict[str, Any] = {
                "version": 2,
                "kind": "chat",
                "platform": platform_norm,
                "chat_id": chat_id_norm,
                "thread_id": thread_id_norm,
                "updated_at": now_iso(),
                "last_delivery_turn_id": last_delivery_turn_id,
            }
            if conversation_key_norm:
                payload["conversation_key"] = conversation_key_norm
            self._save_unlocked(payload)
        return payload

    def clear(self) -> None:
        with file_lock(self._lock_path()):
            try:
                self._path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                logger.warning("Failed to clear PMA active sink: %s", exc)

    def mark_delivered(self, turn_id: str) -> bool:
        if not isinstance(turn_id, str) or not turn_id:
            return False
        with file_lock(self._lock_path()):
            payload = self._load_unlocked()
            if not isinstance(payload, dict):
                return False
            if payload.get("last_delivery_turn_id") == turn_id:
                return False
            payload["last_delivery_turn_id"] = turn_id
            payload["updated_at"] = now_iso()
            self._save_unlocked(payload)
        return True

    def _load_unlocked(self) -> Optional[dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read PMA active sink: %s", exc)
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _save_unlocked(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(payload, indent=2) + "\n")


__all__ = ["PmaActiveSinkStore", "PMA_ACTIVE_SINK_FILENAME"]
