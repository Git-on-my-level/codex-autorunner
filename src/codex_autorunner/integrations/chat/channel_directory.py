from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.locks import file_lock
from ...core.time_utils import now_iso
from ...core.utils import atomic_write

CHANNEL_DIRECTORY_FILENAME = "channel_directory.json"

logger = logging.getLogger(__name__)


def default_channel_directory_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "chat" / CHANNEL_DIRECTORY_FILENAME


def default_channel_directory_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "entries": [],
    }


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


def _normalize_meta_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            key_text = str(key).strip()
            if not key_text:
                continue
            result[key_text] = _normalize_meta_value(value[key])
        return result
    if isinstance(value, list):
        return [_normalize_meta_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_meta_value(item) for item in value]
    return str(value)


def _normalize_meta(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(meta):
        key_text = str(key).strip()
        if not key_text:
            continue
        result[key_text] = _normalize_meta_value(meta[key])
    return result


def normalize_channel_entry(entry: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    platform = _optional_text(entry.get("platform"), lower=True)
    chat_id = _optional_text_or_int(entry.get("chat_id"))
    if platform is None or chat_id is None:
        return None

    display = _optional_text(entry.get("display"))
    if display is None:
        display = f"{platform}:{chat_id}"

    normalized: dict[str, Any] = {
        "platform": platform,
        "chat_id": chat_id,
    }
    thread_id = _optional_text_or_int(entry.get("thread_id"))
    if thread_id is not None:
        normalized["thread_id"] = thread_id
    normalized["display"] = display
    seen_at = _optional_text(entry.get("seen_at")) or now_iso()
    normalized["seen_at"] = seen_at
    normalized["meta"] = _normalize_meta(entry.get("meta"))
    return normalized


def channel_entry_key(entry: Mapping[str, Any]) -> Optional[str]:
    normalized = normalize_channel_entry(entry)
    if normalized is None:
        return None
    platform = normalized["platform"]
    chat_id = normalized["chat_id"]
    thread_id = normalized.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return f"{platform}:{chat_id}:{thread_id}"
    return f"{platform}:{chat_id}"


def _seen_at_value(entry: Mapping[str, Any]) -> str:
    seen_at = entry.get("seen_at")
    if isinstance(seen_at, str):
        return seen_at
    return ""


def _entry_sort_desc(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Stable sort: newest first, and for identical timestamps use key ascending.
    keyed = sorted(entries, key=lambda item: channel_entry_key(item) or "")
    return sorted(keyed, key=_seen_at_value, reverse=True)


def _entry_sort_asc(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed = sorted(entries, key=lambda item: channel_entry_key(item) or "")
    return sorted(keyed, key=_seen_at_value)


class ChannelDirectoryStore:
    def __init__(self, hub_root: Path, *, max_entries: int = 2000) -> None:
        self._path = default_channel_directory_path(hub_root)
        self._max_entries = max(1, int(max_entries))

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        with file_lock(self._lock_path()):
            return self._load_unlocked()

    def record_seen(
        self,
        platform: str,
        chat_id: str | int,
        thread_id: Optional[str | int],
        display: str,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        seen_at = now_iso()
        raw_entry: dict[str, Any] = {
            "platform": platform,
            "chat_id": chat_id,
            "display": display,
            "seen_at": seen_at,
            "meta": dict(meta or {}),
        }
        if thread_id is not None:
            raw_entry["thread_id"] = thread_id
        normalized = normalize_channel_entry(raw_entry)
        if normalized is None:
            raise ValueError("channel entry is invalid")
        key = channel_entry_key(normalized)
        if key is None:
            raise ValueError("channel entry is invalid")

        with file_lock(self._lock_path()):
            payload = self._load_unlocked()
            entries = list(payload["entries"])
            replaced = False
            for index, candidate in enumerate(entries):
                if channel_entry_key(candidate) == key:
                    entries[index] = normalized
                    replaced = True
                    break
            if not replaced:
                entries.append(normalized)
            entries = self._apply_eviction(entries)
            payload["entries"] = _entry_sort_desc(entries)
            payload["updated_at"] = seen_at
            self._save_unlocked(payload)
            return normalized

    def list_entries(
        self,
        query: Optional[str] = None,
        *,
        limit: Optional[int] = 100,
    ) -> list[dict[str, Any]]:
        state = self.load()
        entries = [
            dict(item) for item in state.get("entries", []) if isinstance(item, dict)
        ]

        query_text = _optional_text(query, lower=True)
        if query_text is not None:
            filtered: list[dict[str, Any]] = []
            for entry in entries:
                haystack = self._entry_search_blob(entry)
                if query_text in haystack:
                    filtered.append(entry)
            entries = filtered

        if limit is None:
            return entries
        if limit <= 0:
            return []
        return entries[:limit]

    def _entry_search_blob(self, entry: Mapping[str, Any]) -> str:
        parts = [
            str(entry.get("platform") or ""),
            str(entry.get("chat_id") or ""),
            str(entry.get("thread_id") or ""),
            str(entry.get("display") or ""),
            json.dumps(entry.get("meta") or {}, sort_keys=True),
        ]
        return " ".join(parts).lower()

    def _apply_eviction(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(entries) <= self._max_entries:
            return entries
        ordered = _entry_sort_asc(entries)
        drop = len(ordered) - self._max_entries
        if drop <= 0:
            return ordered
        return ordered[drop:]

    def _load_unlocked(self) -> dict[str, Any]:
        payload = self._read_json_object(self._path)
        if payload is None:
            return default_channel_directory_state()
        return self._normalize_payload(payload)

    def _normalize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        entries_raw = payload.get("entries")
        candidate_entries = entries_raw if isinstance(entries_raw, list) else []
        normalized_by_key: dict[str, dict[str, Any]] = {}
        for item in candidate_entries:
            if not isinstance(item, Mapping):
                continue
            normalized = normalize_channel_entry(item)
            if normalized is None:
                continue
            key = channel_entry_key(normalized)
            if key is None:
                continue
            existing = normalized_by_key.get(key)
            if existing is None:
                normalized_by_key[key] = normalized
                continue
            existing_seen = _seen_at_value(existing)
            candidate_seen = _seen_at_value(normalized)
            if candidate_seen > existing_seen:
                normalized_by_key[key] = normalized

        entries = _entry_sort_desc(list(normalized_by_key.values()))
        entries = self._apply_eviction(entries)

        updated_at = _optional_text(payload.get("updated_at")) or now_iso()
        return {
            "version": 1,
            "updated_at": updated_at,
            "entries": entries,
        }

    def _read_json_object(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read channel directory from %s: %s", path, exc)
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
        atomic_write(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


__all__ = [
    "CHANNEL_DIRECTORY_FILENAME",
    "ChannelDirectoryStore",
    "channel_entry_key",
    "default_channel_directory_path",
    "default_channel_directory_state",
    "normalize_channel_entry",
]
