from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional, cast

from ..state_roots import resolve_hub_traces_root
from ..text_utils import _json_dumps
from ..time_utils import now_iso
from .execution_history import (
    CheckpointSignalStatus,
    ExecutionCheckpoint,
    ExecutionHistoryEventFamily,
    ExecutionTraceManifest,
    ExecutionTraceManifestStatus,
)
from .sqlite import open_orchestration_sqlite

_TRACE_FORMAT = "gzipped_jsonl"
_TRACE_SCHEMA_VERSION = 1
_TRACE_GLOB_SUFFIX = ".trace.jsonl.gz"


def _trace_artifact_relpath(execution_id: str, trace_id: str) -> str:
    return f"{execution_id}/{trace_id}{_TRACE_GLOB_SUFFIX}"


def _build_trace_envelope(
    *,
    seq: int,
    event_family: ExecutionHistoryEventFamily,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "seq": seq,
        "timestamp": now_iso(),
        "event_family": event_family,
        "event_type": event_type,
        "payload": payload,
    }


def _serialize_envelope(envelope: dict[str, Any]) -> bytes:
    line = json.dumps(
        envelope, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return (line + "\n").encode("utf-8")


class ColdTraceWriter:
    """Append-only writer for a single execution's cold trace artifact.

    The writer keeps a gzip file open and appends JSONL event envelopes.
    Callers must call ``finalize()`` when the execution ends to persist the
    manifest; ``close()`` is also available for cleanup without finalization.
    """

    def __init__(
        self,
        *,
        hub_root: Path,
        execution_id: str,
        trace_id: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
    ) -> None:
        self._hub_root = hub_root
        self._execution_id = execution_id
        self._trace_id = trace_id or uuid.uuid4().hex[:24]
        self._backend_thread_id = backend_thread_id
        self._backend_turn_id = backend_turn_id
        self._traces_root = resolve_hub_traces_root(hub_root)
        self._relpath = _trace_artifact_relpath(execution_id, self._trace_id)
        self._artifact_path = self._traces_root / self._relpath
        self._seq: int = 0
        self._event_count: int = 0
        self._byte_count: int = 0
        self._includes_families: set[str] = set()
        self._started_at: Optional[str] = None
        self._finished_at: Optional[str] = None
        self._manifest: Optional[ExecutionTraceManifest] = None
        self._file: Optional[gzip.GzipFile] = None

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def manifest(self) -> Optional[ExecutionTraceManifest]:
        return self._manifest

    def open(self) -> "ColdTraceWriter":
        self._artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = gzip.open(self._artifact_path, "ab")
        self._started_at = now_iso()
        return self

    def append(
        self,
        *,
        event_family: ExecutionHistoryEventFamily,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        if self._file is None:
            raise RuntimeError("ColdTraceWriter is not open")
        self._seq += 1
        envelope = _build_trace_envelope(
            seq=self._seq,
            event_family=event_family,
            event_type=event_type,
            payload=payload,
        )
        data = _serialize_envelope(envelope)
        self._file.write(data)
        self._file.flush()
        self._byte_count += len(data)
        self._event_count += 1
        self._includes_families.add(event_family)
        return self._seq

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def finalize(self) -> ExecutionTraceManifest:
        self._finished_at = now_iso()
        self.flush()
        self._close_file()
        checksum = _compute_file_checksum(self._artifact_path)
        byte_count = (
            self._artifact_path.stat().st_size if self._artifact_path.exists() else 0
        )
        manifest = ExecutionTraceManifest(
            trace_id=self._trace_id,
            execution_id=self._execution_id,
            artifact_relpath=self._relpath,
            trace_format=_TRACE_FORMAT,
            event_count=self._event_count,
            byte_count=byte_count,
            checksum=checksum,
            schema_version=_TRACE_SCHEMA_VERSION,
            status="finalized",
            started_at=self._started_at,
            finished_at=self._finished_at,
            backend_thread_id=self._backend_thread_id,
            backend_turn_id=self._backend_turn_id,
            includes_families=cast(
                tuple[ExecutionHistoryEventFamily, ...],
                tuple(sorted(self._includes_families)),
            ),
            redactions_applied=(),
        )
        self._manifest = manifest
        _persist_manifest(self._hub_root, manifest)
        return manifest

    def close(self) -> None:
        self._close_file()

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __enter__(self) -> "ColdTraceWriter":
        return self.open()

    def __exit__(self, *args: Any) -> None:
        self.close()


def _compute_file_checksum(path: Path) -> str:
    sha256 = hashlib.sha256()
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return f"sha256:{sha256.hexdigest()}"


def _persist_manifest(hub_root: Path, manifest: ExecutionTraceManifest) -> None:
    now = now_iso()
    with open_orchestration_sqlite(hub_root) as conn:
        conn.execute(
            """
            INSERT INTO orch_cold_trace_manifests (
                trace_id, execution_id, artifact_relpath, trace_format,
                event_count, byte_count, checksum, schema_version, status,
                started_at, finished_at, backend_thread_id, backend_turn_id,
                includes_families_json, redactions_applied_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id) DO UPDATE SET
                execution_id = excluded.execution_id,
                artifact_relpath = excluded.artifact_relpath,
                trace_format = excluded.trace_format,
                event_count = excluded.event_count,
                byte_count = excluded.byte_count,
                checksum = excluded.checksum,
                schema_version = excluded.schema_version,
                status = excluded.status,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                backend_thread_id = excluded.backend_thread_id,
                backend_turn_id = excluded.backend_turn_id,
                includes_families_json = excluded.includes_families_json,
                redactions_applied_json = excluded.redactions_applied_json,
                updated_at = excluded.updated_at
            """,
            (
                manifest.trace_id,
                manifest.execution_id,
                manifest.artifact_relpath,
                manifest.trace_format,
                manifest.event_count,
                manifest.byte_count,
                manifest.checksum,
                manifest.schema_version,
                manifest.status,
                manifest.started_at,
                manifest.finished_at,
                manifest.backend_thread_id,
                manifest.backend_turn_id,
                _json_dumps(list(manifest.includes_families)),
                _json_dumps(list(manifest.redactions_applied)),
                now,
                now,
            ),
        )


class ColdTraceReader:
    """Read events from a finalized cold trace artifact."""

    @staticmethod
    def read_events(
        hub_root: Path,
        manifest: ExecutionTraceManifest,
        *,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        traces_root = resolve_hub_traces_root(hub_root)
        artifact_path = traces_root / manifest.artifact_relpath
        if not artifact_path.exists():
            return []
        events: list[dict[str, Any]] = []
        count = 0
        with gzip.open(artifact_path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                if count <= offset:
                    continue
                events.append(envelope)
                if limit is not None and len(events) >= limit:
                    break
        return events

    @staticmethod
    def iter_events(
        hub_root: Path,
        manifest: ExecutionTraceManifest,
    ) -> Iterator[dict[str, Any]]:
        traces_root = resolve_hub_traces_root(hub_root)
        artifact_path = traces_root / manifest.artifact_relpath
        if not artifact_path.exists():
            return
        with gzip.open(artifact_path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield envelope


class ColdTraceStore:
    """High-level cold trace store managing trace artifacts, manifests, and checkpoints.

    Cold traces are compressed JSONL files stored under
    ``.codex-autorunner/traces/`` and are addressable by execution id through
    manifest metadata in the orchestration SQLite database.
    """

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root

    def open_writer(
        self,
        *,
        execution_id: str,
        trace_id: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
    ) -> ColdTraceWriter:
        return ColdTraceWriter(
            hub_root=self._hub_root,
            execution_id=execution_id,
            trace_id=trace_id,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )

    def get_manifest(self, execution_id: str) -> Optional[ExecutionTraceManifest]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_cold_trace_manifests
                 WHERE execution_id = ?
                   AND status = 'finalized'
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (execution_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_manifest(row)

    def get_manifest_by_trace_id(
        self, trace_id: str
    ) -> Optional[ExecutionTraceManifest]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_cold_trace_manifests
                 WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_manifest(row)

    def list_manifests(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionTraceManifest]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            if status is not None:
                rows = conn.execute(
                    """
                    SELECT *
                      FROM orch_cold_trace_manifests
                     WHERE status = ?
                     ORDER BY updated_at DESC
                     LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                      FROM orch_cold_trace_manifests
                     ORDER BY updated_at DESC
                     LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [_row_to_manifest(row) for row in rows]

    def read_events(
        self,
        execution_id: str,
        *,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        manifest = self.get_manifest(execution_id)
        if manifest is None:
            return []
        return ColdTraceReader.read_events(
            self._hub_root, manifest, offset=offset, limit=limit
        )

    def iter_events(self, execution_id: str) -> Iterator[dict[str, Any]]:
        manifest = self.get_manifest(execution_id)
        if manifest is None:
            return
        yield from ColdTraceReader.iter_events(self._hub_root, manifest)

    def save_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        if checkpoint.execution_id is None:
            raise ValueError("ExecutionCheckpoint must have an execution_id")
        now = now_iso()
        checkpoint_json = _json_dumps(checkpoint.to_dict())
        trace_manifest_id = checkpoint.trace_manifest_id
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute(
                """
                INSERT INTO orch_execution_checkpoints (
                    execution_id, thread_target_id, status,
                    checkpoint_json, trace_manifest_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    thread_target_id = excluded.thread_target_id,
                    status = excluded.status,
                    checkpoint_json = excluded.checkpoint_json,
                    trace_manifest_id = excluded.trace_manifest_id,
                    updated_at = excluded.updated_at
                """,
                (
                    checkpoint.execution_id,
                    checkpoint.thread_target_id,
                    checkpoint.status,
                    checkpoint_json,
                    trace_manifest_id,
                    now,
                    now,
                ),
            )

    def load_checkpoint(self, execution_id: str) -> Optional[ExecutionCheckpoint]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT checkpoint_json
                  FROM orch_execution_checkpoints
                 WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
        if row is None:
            return None
        return _parse_checkpoint(row["checkpoint_json"])

    def load_latest_checkpoint_for_thread(
        self, thread_target_id: str
    ) -> Optional[ExecutionCheckpoint]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT checkpoint_json
                  FROM orch_execution_checkpoints
                 WHERE thread_target_id = ?
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (thread_target_id,),
            ).fetchone()
        if row is None:
            return None
        return _parse_checkpoint(row["checkpoint_json"])

    def list_checkpoints(
        self,
        *,
        thread_target_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionCheckpoint]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            conditions: list[str] = []
            params: list[Any] = []
            if thread_target_id is not None:
                conditions.append("thread_target_id = ?")
                params.append(thread_target_id)
            if status is not None:
                conditions.append("status = ?")
                params.append(status)
            where = ""
            if conditions:
                where = "WHERE " + " AND ".join(conditions)
            rows = conn.execute(
                f"""
                SELECT checkpoint_json
                  FROM orch_execution_checkpoints
                 {where}
                 ORDER BY updated_at DESC
                 LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [_parse_checkpoint(row["checkpoint_json"]) for row in rows]

    def archive_trace(self, trace_id: str) -> Optional[ExecutionTraceManifest]:
        manifest = self.get_manifest_by_trace_id(trace_id)
        if manifest is None:
            return None
        updated = ExecutionTraceManifest(
            trace_id=manifest.trace_id,
            execution_id=manifest.execution_id,
            artifact_relpath=manifest.artifact_relpath,
            trace_format=manifest.trace_format,
            event_count=manifest.event_count,
            byte_count=manifest.byte_count,
            checksum=manifest.checksum,
            schema_version=manifest.schema_version,
            status="archived",
            started_at=manifest.started_at,
            finished_at=manifest.finished_at,
            backend_thread_id=manifest.backend_thread_id,
            backend_turn_id=manifest.backend_turn_id,
            includes_families=manifest.includes_families,
            redactions_applied=manifest.redactions_applied,
        )
        _persist_manifest(self._hub_root, updated)
        return updated


def _row_to_manifest(row: Any) -> ExecutionTraceManifest:
    includes_raw = json.loads(str(row["includes_families_json"] or "[]"))
    redactions_raw = json.loads(str(row["redactions_applied_json"] or "[]"))
    return ExecutionTraceManifest(
        trace_id=str(row["trace_id"]),
        execution_id=str(row["execution_id"]),
        artifact_relpath=str(row["artifact_relpath"]),
        trace_format=str(row["trace_format"]),
        event_count=int(row["event_count"] or 0),
        byte_count=int(row["byte_count"] or 0),
        checksum=_row_optional(row, "checksum"),
        schema_version=int(row["schema_version"] or 1),
        status=cast(ExecutionTraceManifestStatus, str(row["status"])),
        started_at=_row_optional(row, "started_at"),
        finished_at=_row_optional(row, "finished_at"),
        backend_thread_id=_row_optional(row, "backend_thread_id"),
        backend_turn_id=_row_optional(row, "backend_turn_id"),
        includes_families=cast(
            tuple[ExecutionHistoryEventFamily, ...],
            tuple(
                str(f) for f in (includes_raw if isinstance(includes_raw, list) else [])
            ),
        ),
        redactions_applied=tuple(
            str(r) for r in (redactions_raw if isinstance(redactions_raw, list) else [])
        ),
    )


def _row_optional(row: Any, key: str) -> Optional[str]:
    value = row[key]
    if value is None or not str(value).strip():
        return None
    return str(value)


def _parse_checkpoint(checkpoint_json: str) -> ExecutionCheckpoint:
    data = json.loads(checkpoint_json)
    terminal_signals_raw = data.pop("terminal_signals", [])
    signals = []
    if isinstance(terminal_signals_raw, list):
        for sig in terminal_signals_raw:
            if isinstance(sig, dict):
                from .execution_history import ExecutionCheckpointSignal

                signals.append(
                    ExecutionCheckpointSignal(
                        source=str(sig.get("source", "")),
                        status=cast(
                            CheckpointSignalStatus, str(sig.get("status", "ok"))
                        ),
                        timestamp=str(sig.get("timestamp", "")),
                    )
                )
    data["terminal_signals"] = tuple(signals)
    return ExecutionCheckpoint(
        **{k: v for k, v in data.items() if k in _CHECKPOINT_FIELDS}
    )


_CHECKPOINT_FIELDS = frozenset(ExecutionCheckpoint.__dataclass_fields__.keys())


__all__ = [
    "ColdTraceReader",
    "ColdTraceStore",
    "ColdTraceWriter",
]
