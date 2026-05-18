from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ..locks import process_alive
from ..utils import atomic_write

_STATE_DIR = ".codex-autorunner"
_REGISTRY_DIR = "processes"


def _validate_path_segment(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError(f"{field_name} contains invalid path characters")
    return text


def _validate_optional_str(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when set")
    return value


def _validate_optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int when set")
    return value


def _validate_command(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("command must be a non-empty list[str]")
    command: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("command must be a non-empty list[str]")
        command.append(item)
    return command


def _validate_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be a dict")
    return dict(value)


def _validate_started_at(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("started_at must be an ISO timestamp string")
    text = value.strip()
    parse_value = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise ValueError("started_at must be an ISO timestamp string") from exc
    return text


def _registry_root(repo_root: Path) -> Path:
    return repo_root.resolve() / _STATE_DIR / _REGISTRY_DIR


@dataclass
class ProcessRecord:
    kind: str
    workspace_id: Optional[str]
    pid: Optional[int]
    pgid: Optional[int]
    base_url: Optional[str]
    command: list[str]
    owner_pid: int
    started_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    handle_id: Optional[str] = None

    def validate(self) -> None:
        self.kind = _validate_path_segment(self.kind, "kind")
        self.handle_id = (
            _validate_path_segment(self.handle_id, "handle_id")
            if self.handle_id is not None
            else None
        )
        self.workspace_id = (
            _validate_path_segment(self.workspace_id, "workspace_id")
            if self.workspace_id is not None
            else None
        )
        self.pid = _validate_optional_int(self.pid, "pid")
        self.pgid = _validate_optional_int(self.pgid, "pgid")
        self.base_url = _validate_optional_str(self.base_url, "base_url")
        self.command = _validate_command(self.command)
        if not isinstance(self.owner_pid, int):
            raise ValueError("owner_pid must be an int")
        self.started_at = _validate_started_at(self.started_at)
        self.metadata = _validate_metadata(self.metadata)

    def record_key(self) -> str:
        if self.handle_id:
            return self.handle_id
        if self.workspace_id:
            return self.workspace_id
        if self.pid is not None:
            return str(self.pid)
        raise ValueError(
            "handle_id, workspace_id, or pid is required to derive record filename"
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "kind": self.kind,
            "handle_id": self.handle_id,
            "workspace_id": self.workspace_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "base_url": self.base_url,
            "command": list(self.command),
            "owner_pid": self.owner_pid,
            "started_at": self.started_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessRecord":
        if not isinstance(data, dict):
            raise ValueError("process record payload must be a dict")
        if "kind" not in data:
            raise ValueError("process record missing required field: kind")
        if "command" not in data:
            raise ValueError("process record missing required field: command")
        if "owner_pid" not in data:
            raise ValueError("process record missing required field: owner_pid")
        if "started_at" not in data:
            raise ValueError("process record missing required field: started_at")
        record = cls(
            kind=cast(str, data["kind"]),
            handle_id=cast(Optional[str], data.get("handle_id")),
            workspace_id=cast(Optional[str], data.get("workspace_id")),
            pid=cast(Optional[int], data.get("pid")),
            pgid=cast(Optional[int], data.get("pgid")),
            base_url=cast(Optional[str], data.get("base_url")),
            command=cast(list[str], data["command"]),
            owner_pid=cast(int, data["owner_pid"]),
            started_at=cast(str, data["started_at"]),
            metadata=cast(dict[str, Any], data.get("metadata") or {}),
        )
        record.validate()
        return record


class ProcessRecordStatus(str, Enum):
    LIVE = "live"
    STALE = "stale"
    CORRUPT = "corrupt"
    ORPHANED = "orphaned"


@dataclass(frozen=True)
class ProcessRecordEntry:
    path: Path
    kind: str
    key: str
    status: ProcessRecordStatus
    record: Optional[ProcessRecord] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ProcessRegistrySummary:
    entries: tuple[ProcessRecordEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ProcessRecordStatus}
        for entry in self.entries:
            counts[entry.status.value] += 1
        return counts

    @property
    def records(self) -> list[ProcessRecord]:
        return [entry.record for entry in self.entries if entry.record is not None]


class ProcessRegistryRepository:
    """File-backed process registry with explicit record classification."""

    def __init__(
        self,
        repo_root: Path,
        *,
        owner_is_alive: Callable[[int], bool] = process_alive,
    ) -> None:
        self._repo_root = repo_root
        self._owner_is_alive = owner_is_alive

    def read(self, kind: str, key: str) -> Optional[ProcessRecord]:
        path = _record_path(self._repo_root, kind, key)
        if not path.exists():
            return None
        entry = self._entry_from_path(path, fallback_kind=kind)
        if entry.record is None:
            raise ValueError(entry.error or f"Invalid process record: {path}")
        return entry.record

    def write(self, record: ProcessRecord, *, durable: bool = False) -> Path:
        payload = record.to_dict()
        path = _record_path(self._repo_root, record.kind, record.record_key())
        atomic_write(
            path, json.dumps(payload, indent=2, sort_keys=True) + "\n", durable
        )
        return path

    def delete(self, kind: str, key: str) -> bool:
        path = _record_path(self._repo_root, kind, key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_entries(self, kind: Optional[str] = None) -> list[ProcessRecordEntry]:
        root = _registry_root(self._repo_root)
        if not root.exists():
            return []

        kind_dirs: list[Path]
        if kind is None:
            kind_dirs = [p for p in sorted(root.iterdir()) if p.is_dir()]
        else:
            kind_dir = root / _validate_path_segment(kind, "kind")
            if not kind_dir.exists():
                return []
            kind_dirs = [kind_dir]

        entries: list[ProcessRecordEntry] = []
        for kind_dir in kind_dirs:
            for path in sorted(kind_dir.glob("*.json")):
                entries.append(self._entry_from_path(path, fallback_kind=kind_dir.name))
        return entries

    def summary(self, kind: Optional[str] = None) -> ProcessRegistrySummary:
        return ProcessRegistrySummary(tuple(self.list_entries(kind=kind)))

    def _entry_from_path(self, path: Path, *, fallback_kind: str) -> ProcessRecordEntry:
        key = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = ProcessRecord.from_dict(data)
        except json.JSONDecodeError:
            return ProcessRecordEntry(
                path=path,
                kind=fallback_kind,
                key=key,
                status=ProcessRecordStatus.CORRUPT,
                error=f"Invalid process record JSON: {path}",
            )
        except (OSError, ValueError) as exc:
            return ProcessRecordEntry(
                path=path,
                kind=fallback_kind,
                key=key,
                status=ProcessRecordStatus.CORRUPT,
                error=str(exc),
            )

        if record.kind != fallback_kind or record.record_key() != key:
            return ProcessRecordEntry(
                path=path,
                kind=fallback_kind,
                key=key,
                status=ProcessRecordStatus.ORPHANED,
                record=record,
                error="record payload does not match registry path",
            )
        status = (
            ProcessRecordStatus.LIVE
            if self._owner_is_alive(record.owner_pid)
            else ProcessRecordStatus.STALE
        )
        return ProcessRecordEntry(
            path=path,
            kind=fallback_kind,
            key=key,
            status=status,
            record=record,
        )


def _record_path(repo_root: Path, kind: str, key: str) -> Path:
    clean_kind = _validate_path_segment(kind, "kind")
    clean_key = _validate_path_segment(key, "record key")
    return _registry_root(repo_root) / clean_kind / f"{clean_key}.json"


def write_process_record(
    repo_root: Path, record: ProcessRecord, *, durable: bool = False
) -> Path:
    return ProcessRegistryRepository(repo_root).write(record, durable=durable)


def read_process_record(
    repo_root: Path, kind: str, key: str
) -> Optional[ProcessRecord]:
    return ProcessRegistryRepository(repo_root).read(kind, key)


def list_process_records(
    repo_root: Path, kind: Optional[str] = None
) -> list[ProcessRecord]:
    entries = ProcessRegistryRepository(repo_root).list_entries(kind=kind)
    corrupt = next(
        (entry for entry in entries if entry.status == ProcessRecordStatus.CORRUPT),
        None,
    )
    if corrupt is not None:
        raise ValueError(f"Invalid process record JSON: {corrupt.path}")
    return [entry.record for entry in entries if entry.record is not None]


def summarize_process_registry(
    repo_root: Path,
    kind: Optional[str] = None,
    *,
    owner_is_alive: Callable[[int], bool] = process_alive,
) -> ProcessRegistrySummary:
    return ProcessRegistryRepository(
        repo_root,
        owner_is_alive=owner_is_alive,
    ).summary(kind=kind)


def delete_process_record(repo_root: Path, kind: str, key: str) -> bool:
    return ProcessRegistryRepository(repo_root).delete(kind, key)
