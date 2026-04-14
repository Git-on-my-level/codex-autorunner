from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from ..sqlite_utils import open_sqlite
from ..state_roots import (
    ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME,
    ORCHESTRATION_DB_FILENAME,
    resolve_hub_orchestration_compatibility_metadata_path,
    resolve_hub_orchestration_db_path,
)
from ..time_utils import now_iso
from ..utils import atomic_write
from .migrations import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    current_orchestration_schema_version,
)

# Hub/chat orchestration DB: higher busy_timeout than generic SQLite defaults (#1266).
# Override via CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS (milliseconds, non-negative int).
_DEFAULT_ORCH_BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class OrchestrationCompatibilityMetadata:
    schema_generation: int
    prepared_at: str
    db_path: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "OrchestrationCompatibilityMetadata":
        schema_generation = int(data.get("schema_generation") or 0)
        if schema_generation < 0:
            raise ValueError("schema_generation must be >= 0")
        prepared_at = str(data.get("prepared_at") or "").strip()
        db_path = str(data.get("db_path") or "").strip()
        if not prepared_at:
            raise ValueError("prepared_at is required")
        if not db_path:
            raise ValueError("db_path is required")
        return cls(
            schema_generation=schema_generation,
            prepared_at=prepared_at,
            db_path=db_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_generation": self.schema_generation,
            "prepared_at": self.prepared_at,
            "db_path": self.db_path,
        }


def orchestration_sqlite_busy_timeout_ms() -> int:
    raw = os.environ.get("CAR_ORCHESTRATION_SQLITE_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_ORCH_BUSY_TIMEOUT_MS


def resolve_orchestration_sqlite_path(hub_root: Path) -> Path:
    """Return the canonical hub orchestration SQLite path."""
    return resolve_hub_orchestration_db_path(hub_root)


def resolve_orchestration_compatibility_metadata_path(hub_root: Path) -> Path:
    """Return the canonical orchestration compatibility metadata path."""
    return resolve_hub_orchestration_compatibility_metadata_path(hub_root)


def _write_orchestration_compatibility_metadata(
    hub_root: Path,
    *,
    schema_generation: int,
    prepared_at: Optional[str] = None,
) -> OrchestrationCompatibilityMetadata:
    metadata = OrchestrationCompatibilityMetadata(
        schema_generation=max(0, int(schema_generation)),
        prepared_at=prepared_at or now_iso(),
        db_path=str(resolve_orchestration_sqlite_path(hub_root)),
    )
    metadata_path = resolve_orchestration_compatibility_metadata_path(hub_root)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(metadata_path, json.dumps(metadata.to_dict(), indent=2) + "\n")
    return metadata


def read_orchestration_compatibility_metadata(
    hub_root: Path,
) -> OrchestrationCompatibilityMetadata | None:
    metadata_path = resolve_orchestration_compatibility_metadata_path(hub_root)
    if not metadata_path.exists():
        return None
    try:
        raw = metadata_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return OrchestrationCompatibilityMetadata.from_mapping(parsed)
    except (TypeError, ValueError):
        return None


def initialize_orchestration_sqlite(hub_root: Path, *, durable: bool = True) -> Path:
    """Create or migrate the canonical orchestration SQLite database."""
    db_path = resolve_orchestration_sqlite_path(hub_root)
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        apply_orchestration_migrations(conn)
        _write_orchestration_compatibility_metadata(
            hub_root,
            schema_generation=current_orchestration_schema_version(conn),
        )
    return db_path


def prepare_orchestration_sqlite(
    hub_root: Path, *, durable: bool = True
) -> OrchestrationCompatibilityMetadata:
    """Explicitly prepare orchestration shared state for hub-owned startup."""
    initialize_orchestration_sqlite(hub_root, durable=durable)
    metadata = read_orchestration_compatibility_metadata(hub_root)
    if metadata is not None:
        return metadata
    return OrchestrationCompatibilityMetadata(
        schema_generation=ORCHESTRATION_SCHEMA_VERSION,
        prepared_at=now_iso(),
        db_path=str(resolve_orchestration_sqlite_path(hub_root)),
    )


@contextmanager
def open_orchestration_sqlite(
    hub_root: Path,
    *,
    durable: bool = True,
    migrate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Open the canonical orchestration SQLite database."""
    db_path = resolve_orchestration_sqlite_path(hub_root)
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        if migrate:
            apply_orchestration_migrations(conn)
        yield conn


__all__ = [
    "ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME",
    "ORCHESTRATION_DB_FILENAME",
    "initialize_orchestration_sqlite",
    "OrchestrationCompatibilityMetadata",
    "open_orchestration_sqlite",
    "orchestration_sqlite_busy_timeout_ms",
    "prepare_orchestration_sqlite",
    "read_orchestration_compatibility_metadata",
    "resolve_orchestration_compatibility_metadata_path",
    "resolve_orchestration_sqlite_path",
]
