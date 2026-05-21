from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from .compatibility import (
    CompatibilityEvaluation,
    CompatibilityRegistry,
    ProcessCompatibilityDeclaration,
    SchemaCompatibilityError,
    build_process_declaration,
    classify_registry_declarations,
    evaluate_schema_compatibility,
    resolve_build_identity,
)
from .migrations import (
    ORCHESTRATION_SCHEMA_VERSION,
    apply_orchestration_migrations,
    current_orchestration_schema_version,
)

# Hub/chat orchestration DB: higher busy_timeout than generic SQLite defaults (#1266).
_DEFAULT_ORCH_BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class OrchestrationCompatibilityMetadata:
    schema_generation: int
    prepared_at: str
    db_path: str
    registry: CompatibilityRegistry = field(default_factory=CompatibilityRegistry)

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
            registry=CompatibilityRegistry.from_mapping(
                registry_payload
                if isinstance(registry_payload := data.get("registry"), dict)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_generation": self.schema_generation,
            "prepared_at": self.prepared_at,
            "db_path": self.db_path,
            "registry": self.registry.to_dict(),
        }


def orchestration_sqlite_busy_timeout_ms() -> int:
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
    declaration: ProcessCompatibilityDeclaration | None = None,
    prepared_at: Optional[str] = None,
) -> OrchestrationCompatibilityMetadata:
    previous = read_orchestration_compatibility_metadata(hub_root)
    registry = previous.registry if previous is not None else CompatibilityRegistry()
    active_declarations, stale_declarations = classify_registry_declarations(
        registry.declarations,
        now_timestamp=time.time(),
    )
    stale_by_id = {
        item.process_id: item
        for item in (*registry.stale_declarations, *stale_declarations)
    }
    registry = CompatibilityRegistry(
        declarations=active_declarations,
        stale_declarations=tuple(stale_by_id.values()),
        updated_at=registry.updated_at,
    )
    if declaration is not None:
        now_text = now_iso()
        active = {
            item.process_id: item
            for item in registry.declarations
            if item.process_id != declaration.process_id
        }
        active[declaration.process_id] = declaration
        registry = CompatibilityRegistry(
            declarations=tuple(active.values()),
            stale_declarations=registry.stale_declarations,
            updated_at=now_text,
        )
    metadata = OrchestrationCompatibilityMetadata(
        schema_generation=max(0, int(schema_generation)),
        prepared_at=prepared_at or now_iso(),
        db_path=str(resolve_orchestration_sqlite_path(hub_root)),
        registry=registry,
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
    hub_root: Path,
    *,
    durable: bool = True,
    process_role: str = "hub",
    control_plane_api_version: str = "1.0.0",
    heartbeat_ttl_seconds: int = 120,
) -> OrchestrationCompatibilityMetadata:
    """Explicitly prepare orchestration shared state for hub-owned startup."""
    db_path = resolve_orchestration_sqlite_path(hub_root)
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        apply_orchestration_migrations(conn)
        schema_generation = current_orchestration_schema_version(conn)
    declaration = build_process_declaration(
        role=process_role,
        supported_control_plane_api_version=control_plane_api_version,
        max_supported_schema_generation=ORCHESTRATION_SCHEMA_VERSION,
        observed_schema_generation=schema_generation,
        ttl_seconds=heartbeat_ttl_seconds,
    )
    return _write_orchestration_compatibility_metadata(
        hub_root,
        schema_generation=schema_generation,
        declaration=declaration,
    )


def evaluate_current_orchestration_compatibility(
    hub_root: Path,
    *,
    process_role: str = "hub",
    supported_schema_generation: int = ORCHESTRATION_SCHEMA_VERSION,
    durable: bool = True,
) -> CompatibilityEvaluation:
    db_path = resolve_orchestration_sqlite_path(hub_root)
    build_id, _unknown_reason = resolve_build_identity()
    with open_sqlite(
        db_path,
        durable=durable,
        busy_timeout_ms=orchestration_sqlite_busy_timeout_ms(),
    ) as conn:
        observed = current_orchestration_schema_version(conn)
    return evaluate_schema_compatibility(
        observed_schema=observed,
        supported_schema=supported_schema_generation,
        process_role=process_role,
        build_id=build_id,
    )


def assert_current_orchestration_compatible(
    hub_root: Path,
    *,
    process_role: str = "hub",
    supported_schema_generation: int = ORCHESTRATION_SCHEMA_VERSION,
    durable: bool = True,
) -> CompatibilityEvaluation:
    evaluation = evaluate_current_orchestration_compatibility(
        hub_root,
        process_role=process_role,
        supported_schema_generation=supported_schema_generation,
        durable=durable,
    )
    if not evaluation.compatible:
        raise SchemaCompatibilityError(evaluation)
    return evaluation


def refresh_orchestration_process_heartbeat(
    hub_root: Path,
    *,
    process_role: str,
    observed_schema_generation: int,
    control_plane_api_version: str = "1.0.0",
    heartbeat_ttl_seconds: int = 120,
) -> OrchestrationCompatibilityMetadata:
    declaration = build_process_declaration(
        role=process_role,
        supported_control_plane_api_version=control_plane_api_version,
        max_supported_schema_generation=ORCHESTRATION_SCHEMA_VERSION,
        observed_schema_generation=observed_schema_generation,
        ttl_seconds=heartbeat_ttl_seconds,
    )
    return _write_orchestration_compatibility_metadata(
        hub_root,
        schema_generation=observed_schema_generation,
        declaration=declaration,
    )


def _read_orchestration_schema_version_if_present(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name = 'orch_schema_migrations'
         LIMIT 1
        """
    ).fetchone()
    if row is None:
        return 0
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM orch_schema_migrations"
    ).fetchone()
    if version_row is None:
        return 0
    return int(version_row["version"] or 0)


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
        else:
            current_version = _read_orchestration_schema_version_if_present(conn)
            if current_version > ORCHESTRATION_SCHEMA_VERSION:
                raise SchemaCompatibilityError(
                    evaluate_schema_compatibility(
                        observed_schema=current_version,
                        supported_schema=ORCHESTRATION_SCHEMA_VERSION,
                        process_role="unknown",
                        build_id="unknown",
                    )
                )
        yield conn


__all__ = [
    "ORCHESTRATION_COMPATIBILITY_METADATA_FILENAME",
    "ORCHESTRATION_DB_FILENAME",
    "assert_current_orchestration_compatible",
    "evaluate_current_orchestration_compatibility",
    "initialize_orchestration_sqlite",
    "OrchestrationCompatibilityMetadata",
    "open_orchestration_sqlite",
    "orchestration_sqlite_busy_timeout_ms",
    "prepare_orchestration_sqlite",
    "read_orchestration_compatibility_metadata",
    "refresh_orchestration_process_heartbeat",
    "resolve_orchestration_compatibility_metadata_path",
    "resolve_orchestration_sqlite_path",
]
