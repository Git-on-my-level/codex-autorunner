from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from . import filebox
from .sqlite_utils import open_sqlite
from .time_utils import now_iso

DeliveryState = Literal[
    "pending",
    "claimed",
    "sending",
    "sent",
    "failed",
    "cancelled",
]

ACTIVE_DELIVERY_STATES: tuple[DeliveryState, ...] = (
    "pending",
    "claimed",
    "sending",
    "failed",
)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    filename: str
    mime_type: str
    size: int
    checksum_sha256: str
    origin_root: str
    storage_path: Path
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DeliveryIntent:
    delivery_id: str
    artifact_id: str
    target_surface: str
    target_conversation_key: str
    workspace_scope: str | None
    state: DeliveryState
    attempts: int
    receipt_json: dict[str, Any] | None
    last_error: str | None
    next_attempt_at: str | None
    claim_token: str | None
    archived_path: str | None
    metadata_json: dict[str, Any]
    created_at: str
    updated_at: str
    claimed_at: str | None
    sent_at: str | None
    failed_at: str | None
    cancelled_at: str | None


@dataclass(frozen=True)
class ClaimedDelivery:
    intent: DeliveryIntent
    artifact: ArtifactRecord
    claim_token: str


class ArtifactDeliveryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    @classmethod
    def for_root(cls, root: Path) -> ArtifactDeliveryStore:
        return cls(artifact_delivery_db_path(root))

    def upsert_artifact_from_path(
        self,
        path: Path,
        *,
        origin_root: Path,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ArtifactRecord:
        source = Path(path)
        stat_result = source.lstat()
        if not stat.S_ISREG(stat_result.st_mode):
            raise ValueError(f"Artifact source must be a regular file: {source}")
        checksum = _sha256_file(source)
        resolved_filename = filebox.sanitize_filename(filename or source.name)
        artifact_id = f"sha256:{checksum}"
        now = now_iso()
        storage_path = artifact_blob_path(self.db_path.parent, checksum)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not storage_path.exists():
            temp_path = storage_path.with_name(
                f".{storage_path.name}.{uuid.uuid4().hex}.tmp"
            )
            shutil.copyfile(source, temp_path)
            temp_path.replace(storage_path)
        with open_sqlite(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id,
                    filename,
                    mime_type,
                    size,
                    checksum_sha256,
                    origin_root,
                    storage_path,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    filename = excluded.filename,
                    mime_type = excluded.mime_type,
                    size = excluded.size,
                    origin_root = excluded.origin_root,
                    storage_path = excluded.storage_path,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    resolved_filename,
                    mime_type or _guess_mime_type(resolved_filename),
                    stat_result.st_size,
                    checksum,
                    str(Path(origin_root)),
                    str(storage_path),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return _artifact_from_row(row)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with open_sqlite(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return None if row is None else _artifact_from_row(row)

    def enqueue_delivery(
        self,
        *,
        artifact_id: str,
        target_surface: str,
        target_conversation_key: str,
        workspace_scope: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryIntent:
        delivery_id = _delivery_key(
            artifact_id=artifact_id,
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
            workspace_scope=workspace_scope,
        )
        now = now_iso()
        with open_sqlite(self.db_path) as conn:
            artifact = conn.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if artifact is None:
                raise ValueError(f"Unknown artifact: {artifact_id}")
            existing = conn.execute(
                "SELECT * FROM delivery_intents WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                return _intent_from_row(existing)
            conn.execute(
                """
                INSERT INTO delivery_intents (
                    delivery_id,
                    artifact_id,
                    target_surface,
                    target_conversation_key,
                    workspace_scope,
                    state,
                    attempts,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    delivery_id,
                    artifact_id,
                    target_surface,
                    target_conversation_key,
                    workspace_scope,
                    _dump_json(metadata or {}),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM delivery_intents WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return _intent_from_row(row)

    def claim_next(
        self,
        *,
        target_surface: str | None = None,
        target_conversation_key: str | None = None,
        claim_token: str | None = None,
        now: str | None = None,
    ) -> ClaimedDelivery | None:
        timestamp = now or now_iso()
        token = claim_token or uuid.uuid4().hex
        predicates = [
            "d.state = 'pending'",
            "(d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)",
        ]
        params: list[Any] = [timestamp]
        if target_surface is not None:
            predicates.append("d.target_surface = ?")
            params.append(target_surface)
        if target_conversation_key is not None:
            predicates.append("d.target_conversation_key = ?")
            params.append(target_conversation_key)
        where = " AND ".join(predicates)
        with open_sqlite(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT d.*
                  FROM delivery_intents d
                 WHERE {where}
                 ORDER BY d.created_at, d.delivery_id
                 LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE delivery_intents
                   SET state = 'claimed',
                       claim_token = ?,
                       claimed_at = ?,
                       updated_at = ?
                 WHERE delivery_id = ?
                   AND state = 'pending'
                """,
                (token, timestamp, timestamp, row["delivery_id"]),
            )
            intent_row = conn.execute(
                "SELECT * FROM delivery_intents WHERE delivery_id = ?",
                (row["delivery_id"],),
            ).fetchone()
            artifact_row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (intent_row["artifact_id"],),
            ).fetchone()
        return ClaimedDelivery(
            intent=_intent_from_row(intent_row),
            artifact=_artifact_from_row(artifact_row),
            claim_token=token,
        )

    def mark_sending(
        self, delivery_id: str, *, claim_token: str | None = None
    ) -> DeliveryIntent:
        return self._transition(
            delivery_id,
            allowed_states=("claimed",),
            updates={"state": "sending"},
            claim_token=claim_token,
        )

    def mark_sent(
        self,
        delivery_id: str,
        *,
        receipt: dict[str, Any] | None = None,
        archived_path: Path | str | None = None,
        claim_token: str | None = None,
    ) -> DeliveryIntent:
        timestamp = now_iso()
        return self._transition(
            delivery_id,
            allowed_states=("claimed", "sending"),
            updates={
                "state": "sent",
                "receipt_json": _dump_json(receipt or {}),
                "archived_path": None if archived_path is None else str(archived_path),
                "sent_at": timestamp,
                "claim_token": None,
                "next_attempt_at": None,
                "last_error": None,
            },
            updated_at=timestamp,
            claim_token=claim_token,
        )

    def mark_failed(
        self,
        delivery_id: str,
        *,
        error: str,
        next_attempt_at: str | None = None,
        claim_token: str | None = None,
    ) -> DeliveryIntent:
        timestamp = now_iso()
        return self._transition(
            delivery_id,
            allowed_states=("claimed", "sending"),
            updates={
                "state": "failed",
                "attempts": ("attempts + 1",),
                "last_error": error,
                "next_attempt_at": next_attempt_at,
                "failed_at": timestamp,
                "claim_token": None,
            },
            updated_at=timestamp,
            claim_token=claim_token,
        )

    def retry(
        self, delivery_id: str, *, next_attempt_at: str | None = None
    ) -> DeliveryIntent:
        return self._transition(
            delivery_id,
            allowed_states=("failed",),
            updates={
                "state": "pending",
                "last_error": None,
                "next_attempt_at": next_attempt_at,
                "claim_token": None,
            },
        )

    def cancel(self, delivery_id: str) -> DeliveryIntent:
        timestamp = now_iso()
        return self._transition(
            delivery_id,
            allowed_states=("pending", "claimed", "sending", "failed"),
            updates={
                "state": "cancelled",
                "cancelled_at": timestamp,
                "claim_token": None,
            },
            updated_at=timestamp,
        )

    def inspect(self, delivery_id: str) -> DeliveryIntent | None:
        with open_sqlite(self.db_path, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM delivery_intents WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return None if row is None else _intent_from_row(row)

    def list_deliveries(
        self,
        *,
        states: Iterable[DeliveryState] | None = None,
        target_surface: str | None = None,
        target_conversation_key: str | None = None,
        workspace_scope: str | None = None,
    ) -> list[DeliveryIntent]:
        where: list[str] = []
        params: list[Any] = []
        if states is not None:
            state_list = list(states)
            if state_list:
                where.append(f"state IN ({','.join('?' for _ in state_list)})")
                params.extend(state_list)
        if target_surface is not None:
            where.append("target_surface = ?")
            params.append(target_surface)
        if target_conversation_key is not None:
            where.append("target_conversation_key = ?")
            params.append(target_conversation_key)
        if workspace_scope is not None:
            where.append("workspace_scope = ?")
            params.append(workspace_scope)
        where_sql = "" if not where else "WHERE " + " AND ".join(where)
        with open_sqlite(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM delivery_intents
                  {where_sql}
                 ORDER BY created_at, delivery_id
                """,
                params,
            ).fetchall()
        return [_intent_from_row(row) for row in rows]

    def import_legacy_outbox(
        self,
        *,
        root: Path,
        target_surface: str,
        target_conversation_key: str,
        workspace_scope: str | None = None,
    ) -> list[DeliveryIntent]:
        filebox.ensure_structure(root)
        imported: list[DeliveryIntent] = []
        for legacy_source, folder in (
            ("outbox", filebox.outbox_dir(root)),
            ("outbox/pending", filebox.outbox_pending_dir(root)),
        ):
            for path in filebox.list_regular_files(folder):
                artifact = self.upsert_artifact_from_path(
                    path,
                    origin_root=root,
                    filename=path.name,
                )
                imported.append(
                    self.enqueue_delivery(
                        artifact_id=artifact.artifact_id,
                        target_surface=target_surface,
                        target_conversation_key=target_conversation_key,
                        workspace_scope=workspace_scope,
                        metadata={
                            "legacy_filebox_source": legacy_source,
                            "legacy_path": str(path),
                        },
                    )
                )
        return imported

    def _transition(
        self,
        delivery_id: str,
        *,
        allowed_states: tuple[str, ...],
        updates: dict[str, Any],
        updated_at: str | None = None,
        claim_token: str | None = None,
    ) -> DeliveryIntent:
        timestamp = updated_at or now_iso()
        assignments = ["updated_at = ?"]
        params: list[Any] = [timestamp]
        for column, value in updates.items():
            if isinstance(value, tuple):
                assignments.append(f"{column} = {value[0]}")
            else:
                assignments.append(f"{column} = ?")
                params.append(value)
        params.append(delivery_id)
        params.extend(allowed_states)
        token_sql = ""
        if claim_token is not None:
            token_sql = " AND claim_token = ?"
            params.append(claim_token)
        with open_sqlite(self.db_path) as conn:
            conn.execute(
                f"""
                UPDATE delivery_intents
                   SET {", ".join(assignments)}
                 WHERE delivery_id = ?
                   AND state IN ({",".join("?" for _ in allowed_states)})
                   {token_sql}
                """,
                params,
            )
            row = conn.execute(
                "SELECT * FROM delivery_intents WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown delivery: {delivery_id}")
        intent = _intent_from_row(row)
        if intent.state != updates["state"] and intent.state in allowed_states:
            raise RuntimeError(f"Delivery transition failed: {delivery_id}")
        return intent

    def _ensure_schema(self) -> None:
        with open_sqlite(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    origin_root TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_intents (
                    delivery_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                    target_surface TEXT NOT NULL,
                    target_conversation_key TEXT NOT NULL,
                    workspace_scope TEXT,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    receipt_json TEXT,
                    last_error TEXT,
                    next_attempt_at TEXT,
                    claim_token TEXT,
                    archived_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    sent_at TEXT,
                    failed_at TEXT,
                    cancelled_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_claim
                    ON delivery_intents(state, next_attempt_at, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_target
                    ON delivery_intents(target_surface, target_conversation_key)
                """
            )


class ArtifactDeliveryService:
    def __init__(self, root: Path, store: ArtifactDeliveryStore | None = None) -> None:
        self.root = Path(root)
        self.store = store or ArtifactDeliveryStore.for_root(self.root)

    def enqueue_file(
        self,
        path: Path,
        *,
        target_surface: str,
        target_conversation_key: str,
        workspace_scope: str | None = None,
        origin_root: Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeliveryIntent:
        artifact = self.store.upsert_artifact_from_path(
            path,
            origin_root=origin_root or self.root,
        )
        return self.store.enqueue_delivery(
            artifact_id=artifact.artifact_id,
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
            workspace_scope=workspace_scope,
            metadata=metadata,
        )

    def import_legacy_outbox(
        self,
        *,
        target_surface: str,
        target_conversation_key: str,
        workspace_scope: str | None = None,
    ) -> list[DeliveryIntent]:
        return self.store.import_legacy_outbox(
            root=self.root,
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
            workspace_scope=workspace_scope,
        )

    def claim_next(
        self,
        *,
        target_surface: str | None = None,
        target_conversation_key: str | None = None,
    ) -> ClaimedDelivery | None:
        return self.store.claim_next(
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
        )

    def mark_sending(
        self, delivery_id: str, *, claim_token: str | None = None
    ) -> DeliveryIntent:
        return self.store.mark_sending(delivery_id, claim_token=claim_token)

    def mark_sent(
        self,
        delivery_id: str,
        *,
        receipt: dict[str, Any] | None = None,
        archived_path: Path | str | None = None,
        claim_token: str | None = None,
    ) -> DeliveryIntent:
        return self.store.mark_sent(
            delivery_id,
            receipt=receipt,
            archived_path=archived_path,
            claim_token=claim_token,
        )

    def mark_failed(
        self,
        delivery_id: str,
        *,
        error: str,
        next_attempt_at: str | None = None,
        claim_token: str | None = None,
    ) -> DeliveryIntent:
        return self.store.mark_failed(
            delivery_id,
            error=error,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
        )

    def retry(
        self, delivery_id: str, *, next_attempt_at: str | None = None
    ) -> DeliveryIntent:
        return self.store.retry(delivery_id, next_attempt_at=next_attempt_at)

    def cancel(self, delivery_id: str) -> DeliveryIntent:
        return self.store.cancel(delivery_id)

    def inspect(self, delivery_id: str) -> DeliveryIntent | None:
        return self.store.inspect(delivery_id)

    def list_deliveries(
        self,
        *,
        states: Iterable[DeliveryState] | None = None,
        target_surface: str | None = None,
        target_conversation_key: str | None = None,
        workspace_scope: str | None = None,
    ) -> list[DeliveryIntent]:
        return self.store.list_deliveries(
            states=states,
            target_surface=target_surface,
            target_conversation_key=target_conversation_key,
            workspace_scope=workspace_scope,
        )

    def inspect_with_artifact(
        self, delivery_id: str
    ) -> tuple[DeliveryIntent, ArtifactRecord | None] | None:
        intent = self.inspect(delivery_id)
        if intent is None:
            return None
        return intent, self.store.get_artifact(intent.artifact_id)


def artifact_delivery_db_path(root: Path) -> Path:
    return Path(root) / ".codex-autorunner" / "artifacts" / "delivery.sqlite3"


def artifact_blob_path(artifact_root: Path, checksum_sha256: str) -> Path:
    return Path(artifact_root) / "blobs" / checksum_sha256[:2] / checksum_sha256


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guess_mime_type(filename: str) -> str:
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _delivery_key(
    *,
    artifact_id: str,
    target_surface: str,
    target_conversation_key: str,
    workspace_scope: str | None,
) -> str:
    payload = json.dumps(
        {
            "artifact_id": artifact_id,
            "target_surface": target_surface,
            "target_conversation_key": target_conversation_key,
            "workspace_scope": workspace_scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"delivery:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=str(row["artifact_id"]),
        filename=str(row["filename"]),
        mime_type=str(row["mime_type"]),
        size=int(row["size"]),
        checksum_sha256=str(row["checksum_sha256"]),
        origin_root=str(row["origin_root"]),
        storage_path=Path(str(row["storage_path"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _intent_from_row(row: sqlite3.Row) -> DeliveryIntent:
    return DeliveryIntent(
        delivery_id=str(row["delivery_id"]),
        artifact_id=str(row["artifact_id"]),
        target_surface=str(row["target_surface"]),
        target_conversation_key=str(row["target_conversation_key"]),
        workspace_scope=row["workspace_scope"],
        state=row["state"],
        attempts=int(row["attempts"]),
        receipt_json=_load_json(row["receipt_json"]),
        last_error=row["last_error"],
        next_attempt_at=row["next_attempt_at"],
        claim_token=row["claim_token"],
        archived_path=row["archived_path"],
        metadata_json=_load_json(row["metadata_json"]) or {},
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        claimed_at=row["claimed_at"],
        sent_at=row["sent_at"],
        failed_at=row["failed_at"],
        cancelled_at=row["cancelled_at"],
    )


def serialize_artifact(artifact: ArtifactRecord | None) -> dict[str, Any] | None:
    if artifact is None:
        return None
    return {
        "artifact_id": artifact.artifact_id,
        "filename": artifact.filename,
        "mime_type": artifact.mime_type,
        "size": artifact.size,
        "checksum_sha256": artifact.checksum_sha256,
        "origin_root": artifact.origin_root,
        "storage_path": str(artifact.storage_path),
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at,
    }


def serialize_delivery(
    intent: DeliveryIntent,
    *,
    artifact: ArtifactRecord | None = None,
) -> dict[str, Any]:
    return {
        "delivery_id": intent.delivery_id,
        "artifact_id": intent.artifact_id,
        "target_surface": intent.target_surface,
        "target_conversation_key": intent.target_conversation_key,
        "workspace_scope": intent.workspace_scope,
        "state": intent.state,
        "attempts": intent.attempts,
        "receipt": intent.receipt_json,
        "last_error": intent.last_error,
        "next_attempt_at": intent.next_attempt_at,
        "archived_path": intent.archived_path,
        "metadata": intent.metadata_json,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
        "claimed_at": intent.claimed_at,
        "sent_at": intent.sent_at,
        "failed_at": intent.failed_at,
        "cancelled_at": intent.cancelled_at,
        "artifact": serialize_artifact(artifact),
    }


def format_delivery_summary(
    service: ArtifactDeliveryService,
    *,
    target_surface: str | None = None,
    target_conversation_key: str | None = None,
    states: Iterable[DeliveryState] | None = None,
    limit: int = 10,
) -> str:
    deliveries = service.list_deliveries(
        states=states,
        target_surface=target_surface,
        target_conversation_key=target_conversation_key,
    )
    if not deliveries:
        return "Artifact deliveries: (none)"
    lines = [f"Artifact deliveries ({len(deliveries)}):"]
    for intent in deliveries[:limit]:
        artifact = service.store.get_artifact(intent.artifact_id)
        name = artifact.filename if artifact is not None else intent.artifact_id
        detail: str = intent.state
        if intent.last_error:
            detail = f"{detail}, {intent.last_error}"
        lines.append(
            f"- {name}: {detail} ({intent.delivery_id[:21]}, attempts {intent.attempts})"
        )
    if len(deliveries) > limit:
        lines.append(f"... and {len(deliveries) - limit} more")
    return "\n".join(lines)


__all__ = [
    "ACTIVE_DELIVERY_STATES",
    "ArtifactDeliveryService",
    "ArtifactDeliveryStore",
    "ArtifactRecord",
    "ClaimedDelivery",
    "DeliveryIntent",
    "DeliveryState",
    "artifact_blob_path",
    "artifact_delivery_db_path",
    "format_delivery_summary",
    "serialize_artifact",
    "serialize_delivery",
]
