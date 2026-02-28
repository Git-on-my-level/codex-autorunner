from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from .locks import file_lock
from .time_utils import now_iso
from .utils import atomic_write

PMA_DELIVERY_TARGETS_FILENAME = "delivery_targets.json"
PMA_ACTIVE_SINK_LEGACY_FILENAME = "active_sink.json"

logger = logging.getLogger(__name__)


def default_delivery_targets_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "targets": [],
        "last_delivery_by_target": {},
    }


def default_delivery_targets_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_DELIVERY_TARGETS_FILENAME


def _optional_text(value: Any, *, lower: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.lower() if lower else normalized


def _optional_text_or_int(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _parse_intish(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and normalized.lstrip("-").isdigit():
            try:
                return int(normalized)
            except ValueError:
                return None
    return None


def _parse_telegram_target_parts(
    chat_id_raw: str, thread_id_raw: Optional[str] = None
) -> Optional[dict[str, Any]]:
    chat_id = _parse_intish(chat_id_raw)
    if chat_id is None:
        return None
    target: dict[str, Any] = {
        "kind": "chat",
        "platform": "telegram",
        "chat_id": str(chat_id),
    }
    if thread_id_raw is not None:
        thread_id = _parse_intish(thread_id_raw)
        if thread_id is None:
            return None
        target["thread_id"] = str(thread_id)
    return target


def _parse_discord_channel_id(chat_id_raw: str) -> Optional[str]:
    chat_id = _parse_intish(chat_id_raw)
    if chat_id is None or chat_id <= 0:
        return None
    return str(chat_id)


def parse_delivery_target_ref(
    ref: str, *, here_target: Optional[Mapping[str, Any]] = None
) -> Optional[dict[str, Any]]:
    value = _optional_text(ref)
    if value is None:
        return None

    lowered = value.lower()
    if lowered == "here":
        if here_target is None:
            return None
        return normalize_delivery_target(here_target)

    candidate: Optional[dict[str, Any]] = None
    if lowered == "web":
        candidate = {"kind": "web"}
    elif lowered.startswith("local:"):
        local_path = value.split(":", 1)[1].strip()
        if not local_path:
            return None
        candidate = {"kind": "local", "path": local_path}
    elif lowered.startswith("telegram:"):
        parts = value.split(":")
        if len(parts) not in {2, 3}:
            return None
        candidate = _parse_telegram_target_parts(
            parts[1], parts[2] if len(parts) == 3 else None
        )
    elif lowered.startswith("discord:"):
        chat_id = _parse_discord_channel_id(value.split(":", 1)[1].strip())
        if chat_id is None:
            return None
        candidate = {"kind": "chat", "platform": "discord", "chat_id": chat_id}
    elif lowered.startswith("chat:"):
        parts = value.split(":")
        if len(parts) not in {3, 4}:
            return None
        platform = parts[1].strip().lower()
        if platform == "telegram":
            candidate = _parse_telegram_target_parts(
                parts[2], parts[3] if len(parts) == 4 else None
            )
        elif platform == "discord":
            if len(parts) != 3:
                return None
            chat_id = _parse_discord_channel_id(parts[2].strip())
            if chat_id is None:
                return None
            candidate = {"kind": "chat", "platform": "discord", "chat_id": chat_id}

    if candidate is None:
        return None
    return normalize_delivery_target(candidate)


def pma_delivery_target_ref_formats(*, include_here: bool = False) -> tuple[str, ...]:
    formats = [
        "web",
        "local:<path>",
        "telegram:<chat_id>[:<thread_id>]",
        "discord:<channel_id>",
        "chat:telegram:<chat_id>[:<thread_id>]",
        "chat:discord:<channel_id>",
    ]
    if include_here:
        formats.insert(0, "here")
    return tuple(formats)


def pma_delivery_target_ref_usage(
    *, include_here: bool = False, multiline: bool = False
) -> str:
    formats = pma_delivery_target_ref_formats(include_here=include_here)
    if multiline:
        return "\n".join(["Refs:", *formats])
    return f"Refs: {' | '.join(formats)}"


def normalize_delivery_target(target: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    kind = _optional_text(target.get("kind"), lower=True)
    if kind == "web":
        return {"kind": "web"}

    if kind == "local":
        path = _optional_text(target.get("path"))
        if path is None:
            return None
        payload: dict[str, Any] = {"kind": "local", "path": path}
        label = _optional_text(target.get("label"))
        if label is not None:
            payload["label"] = label
        return payload

    if kind != "chat":
        return None

    platform = _optional_text(target.get("platform"), lower=True)
    chat_id = _optional_text_or_int(target.get("chat_id"))
    if platform is None or chat_id is None:
        return None
    payload = {
        "kind": "chat",
        "platform": platform,
        "chat_id": chat_id,
    }
    thread_id = _optional_text_or_int(target.get("thread_id"))
    if thread_id is not None:
        payload["thread_id"] = thread_id
    conversation_key = _optional_text(target.get("conversation_key"))
    if conversation_key is not None:
        payload["conversation_key"] = conversation_key
    label = _optional_text(target.get("label"))
    if label is not None:
        payload["label"] = label
    return payload


def target_key(target: Mapping[str, Any]) -> Optional[str]:
    normalized = normalize_delivery_target(target)
    if normalized is None:
        return None
    kind = normalized["kind"]
    if kind == "web":
        return "web"
    if kind == "local":
        return f"local:{normalized['path']}"
    if kind != "chat":
        return None
    platform = normalized["platform"]
    chat_id = normalized["chat_id"]
    thread_id = normalized.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return f"chat:{platform}:{chat_id}:{thread_id}"
    return f"chat:{platform}:{chat_id}"


class PmaDeliveryTargetsStore:
    def __init__(self, hub_root: Path) -> None:
        self._path = default_delivery_targets_path(hub_root)
        pma_root = self._path.parent
        self._legacy_active_sink_path = pma_root / PMA_ACTIVE_SINK_LEGACY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            return self._load_unlocked()

    def set_targets(self, targets: list[Mapping[str, Any]]) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            existing = self._load_unlocked()
            existing_last = dict(existing.get("last_delivery_by_target") or {})
            normalized_targets = self._normalize_targets(targets)
            next_last = {
                key: existing_last[key]
                for key in (target_key(target) for target in normalized_targets)
                if isinstance(key, str) and isinstance(existing_last.get(key), str)
            }
            payload = {
                "version": 1,
                "updated_at": now_iso(),
                "targets": normalized_targets,
                "last_delivery_by_target": next_last,
            }
            self._save_unlocked(payload)
            return payload

    def add_target(self, target: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_delivery_target(target)
        if normalized is None:
            raise ValueError("target is invalid")
        key = target_key(normalized)
        if key is None:
            raise ValueError("target is invalid")

        with file_lock(self._lock_path()):
            payload = self._load_unlocked()
            targets = list(payload["targets"])
            replaced = False
            for index, candidate in enumerate(targets):
                if target_key(candidate) == key:
                    targets[index] = normalized
                    replaced = True
                    break
            if not replaced:
                targets.append(normalized)
            payload["targets"] = targets
            payload["updated_at"] = now_iso()
            self._save_unlocked(payload)
            return payload

    def remove_target(self, target_or_key: Mapping[str, Any] | str) -> bool:
        key: Optional[str]
        if isinstance(target_or_key, str):
            key = target_or_key.strip() or None
        else:
            key = target_key(target_or_key)
        if key is None:
            return False

        with file_lock(self._lock_path()):
            payload = self._load_unlocked()
            targets = list(payload["targets"])
            filtered = [
                candidate for candidate in targets if target_key(candidate) != key
            ]
            if len(filtered) == len(targets):
                return False
            payload["targets"] = filtered
            last_delivery = dict(payload.get("last_delivery_by_target") or {})
            last_delivery.pop(key, None)
            payload["last_delivery_by_target"] = last_delivery
            payload["updated_at"] = now_iso()
            self._save_unlocked(payload)
            return True

    def mark_delivered(
        self, target_or_key: Mapping[str, Any] | str, turn_id: str
    ) -> bool:
        if not isinstance(turn_id, str) or not turn_id:
            return False

        key: Optional[str]
        if isinstance(target_or_key, str):
            key = target_or_key.strip() or None
        else:
            key = target_key(target_or_key)
        if key is None:
            return False

        with file_lock(self._lock_path()):
            payload = self._load_unlocked()
            keys = {
                candidate_key
                for candidate_key in (
                    target_key(candidate) for candidate in payload["targets"]
                )
                if isinstance(candidate_key, str)
            }
            if key not in keys:
                return False
            last_delivery = dict(payload.get("last_delivery_by_target") or {})
            if last_delivery.get(key) == turn_id:
                return False
            last_delivery[key] = turn_id
            payload["last_delivery_by_target"] = last_delivery
            payload["updated_at"] = now_iso()
            self._save_unlocked(payload)
            return True

    def _load_unlocked(self) -> dict[str, Any]:
        payload = self._read_json_object(self._path)
        if payload is None and not self._path.exists():
            payload = self._load_legacy_active_sink_unlocked()
        if payload is None:
            return default_delivery_targets_state()
        return self._normalize_payload(payload)

    def _load_legacy_active_sink_unlocked(self) -> Optional[dict[str, Any]]:
        payload = self._read_json_object(self._legacy_active_sink_path)
        if payload is None:
            return None
        target = self._legacy_payload_to_target(payload)
        last_delivery_by_target: dict[str, str] = {}
        if target is not None:
            key = target_key(target)
            last_delivery_turn_id = payload.get("last_delivery_turn_id")
            if (
                isinstance(key, str)
                and key
                and isinstance(last_delivery_turn_id, str)
                and last_delivery_turn_id
            ):
                last_delivery_by_target[key] = last_delivery_turn_id
        updated_at = _optional_text(payload.get("updated_at")) or now_iso()
        return {
            "version": 1,
            "updated_at": updated_at,
            "targets": [target] if target is not None else [],
            "last_delivery_by_target": last_delivery_by_target,
        }

    def _legacy_payload_to_target(
        self, payload: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        kind = _optional_text(payload.get("kind"), lower=True)
        if kind == "web":
            return {"kind": "web"}
        if kind == "telegram":
            chat_id = _parse_intish(payload.get("chat_id"))
            if chat_id is None:
                return None
            target: dict[str, Any] = {
                "kind": "chat",
                "platform": "telegram",
                "chat_id": str(chat_id),
            }
            thread_id = _parse_intish(payload.get("thread_id"))
            if thread_id is not None:
                target["thread_id"] = str(thread_id)
            topic_key = _optional_text(payload.get("topic_key"))
            if topic_key is not None:
                target["conversation_key"] = topic_key
            return target
        if kind != "chat":
            return None
        normalized_target = normalize_delivery_target(payload)
        if normalized_target is None:
            return None
        return normalized_target

    def _normalize_targets(
        self, targets: list[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for target in targets:
            candidate = normalize_delivery_target(target)
            if candidate is None:
                continue
            key = target_key(candidate)
            if key is None or key in seen:
                continue
            seen.add(key)
            normalized.append(candidate)
        return normalized

    def _normalize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        targets_raw = payload.get("targets")
        candidate_targets = targets_raw if isinstance(targets_raw, list) else []
        normalized_targets: list[dict[str, Any]] = []
        keys: list[str] = []
        seen: set[str] = set()
        for item in candidate_targets:
            if not isinstance(item, Mapping):
                continue
            target = normalize_delivery_target(item)
            if target is None:
                continue
            key = target_key(target)
            if key is None or key in seen:
                continue
            seen.add(key)
            keys.append(key)
            normalized_targets.append(target)

        last_delivery_raw = payload.get("last_delivery_by_target")
        normalized_last_delivery: dict[str, str] = {}
        if isinstance(last_delivery_raw, Mapping):
            for key in keys:
                value = last_delivery_raw.get(key)
                if isinstance(value, str) and value:
                    normalized_last_delivery[key] = value

        updated_at = _optional_text(payload.get("updated_at")) or now_iso()
        return {
            "version": 1,
            "updated_at": updated_at,
            "targets": normalized_targets,
            "last_delivery_by_target": normalized_last_delivery,
        }

    def _read_json_object(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read PMA delivery targets from %s: %s", path, exc)
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _save_unlocked(self, payload: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(payload, indent=2) + "\n")


__all__ = [
    "PMA_DELIVERY_TARGETS_FILENAME",
    "PmaDeliveryTargetsStore",
    "default_delivery_targets_path",
    "default_delivery_targets_state",
    "parse_delivery_target_ref",
    "normalize_delivery_target",
    "pma_delivery_target_ref_formats",
    "pma_delivery_target_ref_usage",
    "target_key",
]
