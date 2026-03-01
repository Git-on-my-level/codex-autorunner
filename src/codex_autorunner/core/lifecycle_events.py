from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .locks import file_lock
from .utils import atomic_write

logger = logging.getLogger(__name__)

LIFECYCLE_EVENTS_FILENAME = "lifecycle_events.json"
LIFECYCLE_EVENTS_LOCK_SUFFIX = ".lock"
TRANSITION_TOKEN_KEY = "transition_token"


class LifecycleEventType(str, Enum):
    FLOW_PAUSED = "flow_paused"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    FLOW_STOPPED = "flow_stopped"
    DISPATCH_CREATED = "dispatch_created"


@dataclass
class LifecycleEvent:
    event_type: LifecycleEventType
    repo_id: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    origin: str = "system"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    processed: bool = False
    event_id: str = ""

    def __post_init__(self):
        if not self.event_id:
            import uuid

            object.__setattr__(self, "event_id", str(uuid.uuid4()))


@dataclass
class LifecycleEventAppendResult:
    event: LifecycleEvent
    deduped: bool = False


def default_lifecycle_events_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / LIFECYCLE_EVENTS_FILENAME


class LifecycleEventStore:
    def __init__(self, hub_root: Path) -> None:
        self._path = default_lifecycle_events_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(LIFECYCLE_EVENTS_LOCK_SUFFIX)

    @staticmethod
    def _is_terminal_flow_event(event_type: LifecycleEventType) -> bool:
        return event_type in {
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        }

    @staticmethod
    def _extract_transition_token(data: dict[str, Any]) -> Optional[str]:
        raw = data.get(TRANSITION_TOKEN_KEY)
        if isinstance(raw, str):
            token = raw.strip()
            if token:
                return token
        return None

    def _semantic_identity(self, event: LifecycleEvent) -> tuple[str, ...]:
        key: tuple[str, ...] = (
            event.event_type.value,
            event.repo_id,
            event.run_id,
        )
        token = self._extract_transition_token(event.data)
        if token:
            key = (*key, token)
        return key

    @staticmethod
    def _parse_duplicate_count(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value if value >= 0 else 0
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
                return parsed if parsed >= 0 else 0
            except Exception:
                return 0
        return 0

    @staticmethod
    def _resolve_first_seen_at(
        existing: LifecycleEvent,
        event_data: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        first_seen = event_data.get("first_seen_at")
        if isinstance(first_seen, str) and first_seen.strip():
            return first_seen.strip()
        if isinstance(existing.timestamp, str) and existing.timestamp.strip():
            return existing.timestamp.strip()
        return fallback

    def _annotate_duplicate(
        self,
        existing: LifecycleEvent,
        *,
        duplicate_seen_at: str,
    ) -> None:
        data = dict(existing.data or {})
        duplicate_count = self._parse_duplicate_count(data.get("duplicate_count"))
        first_seen_at = self._resolve_first_seen_at(
            existing, data, fallback=duplicate_seen_at
        )
        data["duplicate_count"] = duplicate_count + 1
        data["first_seen_at"] = first_seen_at
        data["last_seen_at"] = duplicate_seen_at
        existing.data = data

    def _find_duplicate_terminal_event(
        self, events: list[LifecycleEvent], candidate: LifecycleEvent
    ) -> Optional[LifecycleEvent]:
        if not self._is_terminal_flow_event(candidate.event_type):
            return None
        candidate_key = self._semantic_identity(candidate)
        for existing in events:
            if not self._is_terminal_flow_event(existing.event_type):
                continue
            if self._semantic_identity(existing) == candidate_key:
                return existing
        return None

    def _load_unlocked(self) -> list[LifecycleEvent]:
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read lifecycle events at %s: %s", self._path, exc)
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse lifecycle events at %s: %s", self._path, exc
            )
            return []
        if not isinstance(data, list):
            logger.warning("Lifecycle events data is not a list: %s", self._path)
            return []
        events: list[LifecycleEvent] = []
        for entry in data:
            try:
                if not isinstance(entry, dict):
                    continue
                event_type_str = entry.get("event_type")
                if not isinstance(event_type_str, str):
                    continue
                try:
                    event_type = LifecycleEventType(event_type_str)
                except ValueError:
                    continue
                event_id_raw = entry.get("event_id")
                event_id = str(event_id_raw) if isinstance(event_id_raw, str) else ""
                if not event_id:
                    import uuid

                    event_id = str(uuid.uuid4())
                origin_raw = entry.get("origin")
                origin = (
                    str(origin_raw).strip()
                    if isinstance(origin_raw, str) and origin_raw.strip()
                    else "system"
                )
                event = LifecycleEvent(
                    event_type=event_type,
                    repo_id=str(entry.get("repo_id", "")),
                    run_id=str(entry.get("run_id", "")),
                    data=dict(entry.get("data", {})),
                    origin=origin,
                    timestamp=str(entry.get("timestamp", "")),
                    processed=bool(entry.get("processed", False)),
                    event_id=event_id,
                )
                events.append(event)
            except Exception as exc:
                logger.debug("Failed to parse lifecycle event entry: %s", exc)
                continue
        return events

    def load(self, *, ensure_exists: bool = True) -> list[LifecycleEvent]:
        with file_lock(self._lock_path()):
            return self._load_unlocked()

    def save(self, events: list[LifecycleEvent]) -> None:
        with file_lock(self._lock_path()):
            self._save_unlocked(events)

    def _save_unlocked(self, events: list[LifecycleEvent]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "repo_id": event.repo_id,
                "run_id": event.run_id,
                "data": event.data,
                "origin": event.origin,
                "timestamp": event.timestamp,
                "processed": event.processed,
            }
            for event in events
        ]
        atomic_write(self._path, json.dumps(data, indent=2) + "\n")

    def append_with_result(self, event: LifecycleEvent) -> LifecycleEventAppendResult:
        with file_lock(self._lock_path()):
            events = self._load_unlocked()
            duplicate = self._find_duplicate_terminal_event(events, event)
            if duplicate is not None:
                seen_at = event.timestamp
                if not isinstance(seen_at, str) or not seen_at.strip():
                    seen_at = datetime.now(timezone.utc).isoformat()
                self._annotate_duplicate(duplicate, duplicate_seen_at=seen_at)
                self._save_unlocked(events)
                return LifecycleEventAppendResult(event=duplicate, deduped=True)
            events.append(event)
            self._save_unlocked(events)
            return LifecycleEventAppendResult(event=event, deduped=False)

    def append(self, event: LifecycleEvent) -> None:
        self.append_with_result(event)

    def mark_processed(self, event_id: str) -> Optional[LifecycleEvent]:
        if not event_id:
            return None
        events = self.load(ensure_exists=False)
        updated = None
        for event in events:
            if event.event_id == event_id:
                event.processed = True
                updated = event
                break
        if updated:
            self.save(events)
        return updated

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        events = self.load(ensure_exists=False)
        unprocessed = [e for e in events if not e.processed]
        return unprocessed[:limit]

    def prune_processed(self, *, keep_last: int = 100) -> None:
        events = self.load(ensure_exists=False)
        unprocessed = [e for e in events if not e.processed]
        processed = [e for e in events if e.processed]
        if len(processed) > keep_last:
            processed = processed[-keep_last:]
        self.save(unprocessed + processed)


class LifecycleEventEmitter:
    def __init__(self, hub_root: Path) -> None:
        self._store = LifecycleEventStore(hub_root)
        self._listeners: list[Callable[[LifecycleEvent], None]] = []

    def emit(self, event: LifecycleEvent) -> str:
        append_result = self._store.append_with_result(event)
        if append_result.deduped:
            return append_result.event.event_id
        for listener in self._listeners:
            try:
                listener(append_result.event)
            except Exception as exc:
                logger.exception("Error in lifecycle event listener: %s", exc)
        return append_result.event.event_id

    def emit_flow_paused(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_PAUSED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_completed(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_COMPLETED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_failed(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_FAILED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_stopped(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_STOPPED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_dispatch_created(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.DISPATCH_CREATED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def add_listener(self, listener: Callable[[LifecycleEvent], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[LifecycleEvent], None]) -> None:
        self._listeners = [lst for lst in self._listeners if lst != listener]


__all__ = [
    "LifecycleEventType",
    "LifecycleEvent",
    "LifecycleEventStore",
    "LifecycleEventEmitter",
    "default_lifecycle_events_path",
]
