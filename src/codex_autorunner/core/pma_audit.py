from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock

logger = logging.getLogger(__name__)

PMA_AUDIT_LOG_FILENAME = "audit_log.jsonl"
PMA_AUDIT_LOG_LOCK_SUFFIX = ".lock"


class PmaActionType(str, Enum):
    CHAT_STARTED = "chat_started"
    CHAT_COMPLETED = "chat_completed"
    CHAT_FAILED = "chat_failed"
    CHAT_INTERRUPTED = "chat_interrupted"
    FILE_UPLOADED = "file_uploaded"
    FILE_DOWNLOADED = "file_downloaded"
    FILE_DELETED = "file_deleted"
    FILE_BULK_DELETED = "file_bulk_deleted"
    DOC_UPDATED = "doc_updated"
    DISPATCH_PROCESSED = "dispatch_processed"
    AGENT_ACTION = "agent_action"
    SESSION_NEW = "session_new"
    SESSION_RESET = "session_reset"
    SESSION_STOP = "session_stop"
    SESSION_COMPACT = "session_compact"
    UNKNOWN = "unknown"


@dataclass
class PmaAuditEntry:
    action_type: PmaActionType
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    entry_id: str = ""
    agent: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    client_turn_id: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error: Optional[str] = None
    fingerprint: str = ""

    def __post_init__(self):
        if not self.entry_id:
            import uuid

            object.__setattr__(self, "entry_id", str(uuid.uuid4()))
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", self._compute_fingerprint())

    def _compute_fingerprint(self) -> str:
        base = {
            "action_type": self.action_type.value,
            "agent": self.agent,
            "details": self.details,
        }
        raw = json.dumps(base, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def default_pma_audit_log_path(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_AUDIT_LOG_FILENAME


class PmaAuditLog:
    def __init__(self, hub_root: Path) -> None:
        self._path = default_pma_audit_log_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return self._path.with_suffix(PMA_AUDIT_LOG_LOCK_SUFFIX)

    def append(self, entry: PmaAuditEntry) -> str:
        with file_lock(self._lock_path()):
            self._append_unlocked(entry)
        return entry.entry_id

    def _append_unlocked(self, entry: PmaAuditEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "entry_id": entry.entry_id,
                "action_type": entry.action_type.value,
                "timestamp": entry.timestamp,
                "agent": entry.agent,
                "thread_id": entry.thread_id,
                "turn_id": entry.turn_id,
                "client_turn_id": entry.client_turn_id,
                "details": entry.details,
                "status": entry.status,
                "error": entry.error,
                "fingerprint": entry.fingerprint,
            }
        )
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def list_recent(
        self, *, limit: int = 100, action_type: Optional[PmaActionType] = None
    ) -> list[PmaAuditEntry]:
        with file_lock(self._lock_path()):
            return self._list_recent_unlocked(limit=limit, action_type=action_type)

    def _list_recent_unlocked(
        self, *, limit: int = 100, action_type: Optional[PmaActionType] = None
    ) -> list[PmaAuditEntry]:
        if not self._path.exists():
            return []
        entries: list[PmaAuditEntry] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    try:
                        action_type_str = data.get("action_type")
                        event_type = (
                            PmaActionType(action_type_str)
                            if action_type_str
                            else PmaActionType.UNKNOWN
                        )
                    except ValueError:
                        event_type = PmaActionType.UNKNOWN
                    if action_type and event_type != action_type:
                        continue
                    entry = PmaAuditEntry(
                        action_type=event_type,
                        timestamp=data.get("timestamp", ""),
                        entry_id=data.get("entry_id", ""),
                        agent=data.get("agent"),
                        thread_id=data.get("thread_id"),
                        turn_id=data.get("turn_id"),
                        client_turn_id=data.get("client_turn_id"),
                        details=dict(data.get("details", {}) or {}),
                        status=data.get("status", "ok"),
                        error=data.get("error"),
                        fingerprint=data.get("fingerprint", ""),
                    )
                    entries.append(entry)
        except OSError as exc:
            logger.warning("Failed to read PMA audit log at %s: %s", self._path, exc)
        return entries[-limit:]

    def prune_old(self, *, keep_last: int = 1000) -> int:
        with file_lock(self._lock_path()):
            return self._prune_old_unlocked(keep_last=keep_last)

    def _prune_old_unlocked(self, *, keep_last: int = 1000) -> int:
        if not self._path.exists():
            return 0
        entries = self._list_recent_unlocked(limit=keep_last * 2)
        if len(entries) <= keep_last:
            return 0
        to_keep = entries[-keep_last:]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            for entry in to_keep:
                line = json.dumps(
                    {
                        "entry_id": entry.entry_id,
                        "action_type": entry.action_type.value,
                        "timestamp": entry.timestamp,
                        "agent": entry.agent,
                        "thread_id": entry.thread_id,
                        "turn_id": entry.turn_id,
                        "client_turn_id": entry.client_turn_id,
                        "details": entry.details,
                        "status": entry.status,
                        "error": entry.error,
                        "fingerprint": entry.fingerprint,
                    }
                )
                f.write(line + "\n")
        return len(entries) - keep_last

    def count_fingerprint(
        self, fingerprint: str, *, within_seconds: Optional[int] = None
    ) -> int:
        if not within_seconds:
            return sum(
                1
                for e in self._list_recent_unlocked(limit=10000)
                if e.fingerprint == fingerprint
            )
        cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
        count = 0
        for entry in self._list_recent_unlocked(limit=10000):
            try:
                ts = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
                if ts.timestamp() >= cutoff and entry.fingerprint == fingerprint:
                    count += 1
            except Exception:
                continue
        return count


__all__ = [
    "PmaActionType",
    "PmaAuditEntry",
    "PmaAuditLog",
    "default_pma_audit_log_path",
]
