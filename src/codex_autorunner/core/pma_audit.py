from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .locks import file_lock
from .orchestration.legacy_backfill_gate import ensure_legacy_orchestration_backfill
from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import lock_path_for

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
    INBOX_AUTO_DISMISSED = "inbox_auto_dismissed"
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
        self._hub_root = hub_root
        self._path = default_pma_audit_log_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    def append(self, entry: PmaAuditEntry) -> str:
        self._append_sqlite(entry)
        with file_lock(self._lock_path()):
            self._append_unlocked(entry)
        return entry.entry_id

    def _append_sqlite(self, entry: PmaAuditEntry) -> None:
        stored_audit_id = f"{entry.entry_id}:{time.time_ns()}"
        payload = {
            "entry_id": entry.entry_id,
            "thread_id": entry.thread_id,
            "turn_id": entry.turn_id,
            "client_turn_id": entry.client_turn_id,
            "details": entry.details,
            "status": entry.status,
            "error": entry.error,
            "agent": entry.agent,
        }
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute(
                """
                INSERT INTO orch_audit_entries (
                    audit_id,
                    action_type,
                    actor_kind,
                    actor_id,
                    target_kind,
                    target_id,
                    repo_id,
                    payload_json,
                    created_at,
                    fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(audit_id) DO UPDATE SET
                    action_type = excluded.action_type,
                    actor_kind = excluded.actor_kind,
                    actor_id = excluded.actor_id,
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    repo_id = excluded.repo_id,
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at,
                    fingerprint = excluded.fingerprint
                """,
                (
                    stored_audit_id,
                    entry.action_type.value,
                    "agent" if entry.agent else None,
                    entry.agent,
                    "thread_target" if entry.thread_id else None,
                    entry.thread_id,
                    None,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    entry.timestamp,
                    entry.fingerprint,
                ),
            )

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
        entries = self._list_recent_sqlite(limit=limit, action_type=action_type)
        if entries:
            return entries
        self._backfill_legacy_entries()
        entries = self._list_recent_sqlite(limit=limit, action_type=action_type)
        if entries:
            return entries
        with file_lock(self._lock_path()):
            return self._list_recent_unlocked(limit=limit, action_type=action_type)

    def _list_recent_sqlite(
        self, *, limit: int = 100, action_type: Optional[PmaActionType] = None
    ) -> list[PmaAuditEntry]:
        clauses = []
        params: list[Any] = []
        if action_type is not None:
            clauses.append("action_type = ?")
            params.append(action_type.value)
        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        with open_orchestration_sqlite(self._hub_root) as conn:
            rows = conn.execute(
                f"""
                SELECT audit_id, action_type, actor_id, target_id, payload_json, created_at, fingerprint
                  FROM orch_audit_entries
                  {where}
                 ORDER BY rowid DESC
                 LIMIT ?
                """,
                (*params, max(0, int(limit))),
            ).fetchall()
        entries: list[PmaAuditEntry] = []
        for row in reversed(rows):
            parsed = self._row_to_entry(row)
            if parsed is not None:
                entries.append(parsed)
        return entries

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
        removed = self._prune_old_sqlite(keep_last=keep_last)
        with file_lock(self._lock_path()):
            removed = max(removed, self._prune_old_unlocked(keep_last=keep_last))
        return removed

    def _prune_old_sqlite(self, *, keep_last: int = 1000) -> int:
        safe_keep_last = max(0, int(keep_last))
        with open_orchestration_sqlite(self._hub_root) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM orch_audit_entries"
            ).fetchone()
            total = int(total_row["c"] or 0) if total_row is not None else 0
            if total <= safe_keep_last:
                return 0
            if safe_keep_last == 0:
                conn.execute("DELETE FROM orch_audit_entries")
                return total
            conn.execute(
                """
                DELETE FROM orch_audit_entries
                 WHERE audit_id NOT IN (
                    SELECT audit_id
                      FROM orch_audit_entries
                     ORDER BY rowid DESC
                     LIMIT ?
                 )
                """,
                (safe_keep_last,),
            )
        return max(0, total - safe_keep_last)

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
        entries = self._list_recent_sqlite(limit=10000)
        if not entries:
            self._backfill_legacy_entries()
            entries = self._list_recent_sqlite(limit=10000)
        if not entries:
            entries = self._list_recent_unlocked(limit=10000)
        if not within_seconds:
            return sum(1 for e in entries if e.fingerprint == fingerprint)
        cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
        count = 0
        for entry in entries:
            try:
                ts = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
                if ts.timestamp() >= cutoff and entry.fingerprint == fingerprint:
                    count += 1
            except (ValueError, TypeError, AttributeError):
                continue
        return count

    def _backfill_legacy_entries(self) -> None:
        ensure_legacy_orchestration_backfill(self._hub_root)

    @staticmethod
    def _row_to_entry(row: Any) -> Optional[PmaAuditEntry]:
        try:
            payload_raw = row["payload_json"]
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else {}
            if not isinstance(payload, dict):
                payload = {}
            action_type = PmaActionType(str(row["action_type"]))
        except ValueError:
            action_type = PmaActionType.UNKNOWN
        except (KeyError, TypeError):
            return None
        return PmaAuditEntry(
            action_type=action_type,
            timestamp=str(row["created_at"] or ""),
            entry_id=(
                str(payload.get("entry_id") or "").strip() or str(row["audit_id"] or "")
            ),
            agent=(
                payload.get("agent") if isinstance(payload.get("agent"), str) else None
            ),
            thread_id=(
                payload.get("thread_id")
                if isinstance(payload.get("thread_id"), str)
                else None
            ),
            turn_id=(
                payload.get("turn_id")
                if isinstance(payload.get("turn_id"), str)
                else None
            ),
            client_turn_id=(
                payload.get("client_turn_id")
                if isinstance(payload.get("client_turn_id"), str)
                else None
            ),
            details=dict(payload.get("details", {}) or {}),
            status=str(payload.get("status") or "ok"),
            error=(
                payload.get("error") if isinstance(payload.get("error"), str) else None
            ),
            fingerprint=str(row["fingerprint"] or ""),
        )


__all__ = [
    "PmaActionType",
    "PmaAuditEntry",
    "PmaAuditLog",
    "default_pma_audit_log_path",
]
