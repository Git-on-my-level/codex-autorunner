from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .pma_delivery_targets import PmaDeliveryTargetsStore, target_key
from .time_utils import now_iso

PMA_ACTIVE_SINK_FILENAME = "active_sink.json"

logger = logging.getLogger(__name__)


class PmaActiveSinkStore:
    def __init__(self, hub_root: Path) -> None:
        self._path = hub_root / ".codex-autorunner" / "pma" / PMA_ACTIVE_SINK_FILENAME
        self._delivery_targets = PmaDeliveryTargetsStore(hub_root)

    def load(self) -> Optional[dict[str, Any]]:
        return self._payload_from_state(self._delivery_targets.load())

    def set_web(self) -> dict[str, Any]:
        state = self._delivery_targets.set_targets([{"kind": "web"}])
        payload = self._payload_from_state(state)
        if payload is not None:
            return payload
        return {
            "version": 1,
            "kind": "web",
            "updated_at": now_iso(),
            "last_delivery_turn_id": None,
        }

    def set_telegram(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        topic_key: Optional[str] = None,
    ) -> dict[str, Any]:
        target: dict[str, Any] = {
            "kind": "chat",
            "platform": "telegram",
            "chat_id": str(int(chat_id)),
        }
        if thread_id is not None:
            target["thread_id"] = str(int(thread_id))
        if topic_key:
            target["conversation_key"] = topic_key
        state = self._delivery_targets.set_targets([target])
        updated_at = state.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = now_iso()

        payload: dict[str, Any] = {
            "version": 1,
            "kind": "telegram",
            "chat_id": int(chat_id),
            "thread_id": int(thread_id) if thread_id is not None else None,
            "updated_at": updated_at,
            "last_delivery_turn_id": self._last_delivery_for_target(
                target, state=state
            ),
        }
        if topic_key:
            payload["topic_key"] = topic_key
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

        target: dict[str, Any] = {
            "kind": "chat",
            "platform": platform_norm,
            "chat_id": chat_id_norm,
        }
        if thread_id_norm:
            target["thread_id"] = thread_id_norm
        if conversation_key_norm:
            target["conversation_key"] = conversation_key_norm

        state = self._delivery_targets.set_targets([target])
        updated_at = state.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = now_iso()

        payload: dict[str, Any] = {
            "version": 2,
            "kind": "chat",
            "platform": platform_norm,
            "chat_id": chat_id_norm,
            "thread_id": thread_id_norm,
            "updated_at": updated_at,
            "last_delivery_turn_id": self._last_delivery_for_target(
                target, state=state
            ),
        }
        if conversation_key_norm:
            payload["conversation_key"] = conversation_key_norm
        return payload

    def clear(self) -> None:
        self._delivery_targets.set_targets([])
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Failed to clear PMA active sink: %s", exc)

    def mark_delivered(self, turn_id: str) -> bool:
        if not isinstance(turn_id, str) or not turn_id:
            return False
        state = self._delivery_targets.load()
        target = self._active_target(state)
        if target is None:
            return False
        key = target_key(target)
        if not isinstance(key, str):
            return False
        return self._delivery_targets.mark_delivered(key, turn_id)

    def _payload_from_state(self, state: dict[str, Any]) -> Optional[dict[str, Any]]:
        target = self._active_target(state)
        if target is None:
            return None
        key = target_key(target)
        last_delivery = None
        if isinstance(key, str):
            raw_last = (state.get("last_delivery_by_target") or {}).get(key)
            if isinstance(raw_last, str) and raw_last:
                last_delivery = raw_last
        updated_at = state.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = now_iso()
        return self._to_legacy_payload(
            target=target,
            updated_at=updated_at,
            last_delivery_turn_id=last_delivery,
        )

    def _active_target(self, state: dict[str, Any]) -> Optional[dict[str, Any]]:
        targets = state.get("targets")
        if not isinstance(targets, list):
            return None
        for target in targets:
            if isinstance(target, dict):
                return target
        return None

    def _last_delivery_for_target(
        self, target: dict[str, Any], *, state: Optional[dict[str, Any]] = None
    ) -> Optional[str]:
        if state is None:
            state = self._delivery_targets.load()
        key = target_key(target)
        if not isinstance(key, str):
            return None
        last_delivery_turn_id = (state.get("last_delivery_by_target") or {}).get(key)
        if isinstance(last_delivery_turn_id, str) and last_delivery_turn_id:
            return last_delivery_turn_id
        return None

    def _to_legacy_payload(
        self,
        *,
        target: dict[str, Any],
        updated_at: str,
        last_delivery_turn_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        kind = target.get("kind")
        if kind == "web":
            return {
                "version": 1,
                "kind": "web",
                "updated_at": updated_at,
                "last_delivery_turn_id": last_delivery_turn_id,
            }
        if kind != "chat":
            return None

        platform = target.get("platform")
        chat_id = target.get("chat_id")
        thread_id = target.get("thread_id")
        conversation_key = target.get("conversation_key")

        if not isinstance(platform, str) or not platform:
            return None
        if not isinstance(chat_id, str) or not chat_id:
            return None

        if platform == "telegram":
            try:
                chat_id_int = int(chat_id)
            except ValueError:
                chat_id_int = None

            if chat_id_int is not None:
                thread_id_int: Optional[int] = None
                if isinstance(thread_id, str) and thread_id:
                    try:
                        thread_id_int = int(thread_id)
                    except ValueError:
                        thread_id_int = None

                payload: dict[str, Any] = {
                    "version": 1,
                    "kind": "telegram",
                    "chat_id": chat_id_int,
                    "thread_id": thread_id_int,
                    "updated_at": updated_at,
                    "last_delivery_turn_id": last_delivery_turn_id,
                }
                if isinstance(conversation_key, str) and conversation_key:
                    payload["topic_key"] = conversation_key
                return payload

        chat_payload: dict[str, Any] = {
            "version": 2,
            "kind": "chat",
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": (
                thread_id if isinstance(thread_id, str) and thread_id else None
            ),
            "updated_at": updated_at,
            "last_delivery_turn_id": last_delivery_turn_id,
        }
        if isinstance(conversation_key, str) and conversation_key:
            chat_payload["conversation_key"] = conversation_key
        return chat_payload


__all__ = ["PmaActiveSinkStore", "PMA_ACTIVE_SINK_FILENAME"]
