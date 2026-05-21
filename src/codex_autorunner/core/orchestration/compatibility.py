from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from typing import Any, Literal, Mapping, Optional

from ..time_utils import now_iso

ProcessRole = Literal["hub", "worker", "discord", "telegram", "cli", "web", "pma"]
CompatibilityStatus = Literal[
    "compatible",
    "restart_required",
    "incompatible_schema",
    "unknown",
]


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def resolve_build_identity() -> tuple[str, Optional[str]]:
    for distribution_name in ("codex-autorunner", "codex_autorunner"):
        try:
            version = importlib_metadata.version(distribution_name).strip()
        except importlib_metadata.PackageNotFoundError:
            continue
        if version:
            return version, None
    return "unknown", "package_version_unavailable"


@dataclass(frozen=True)
class ProcessCompatibilityDeclaration:
    process_id: str
    role: str
    pid: int
    process_start_time: float
    build_id: str
    unknown_build_reason: Optional[str]
    writer_identity: str
    supported_control_plane_api_version: str
    max_supported_schema_generation: int
    observed_schema_generation: int
    heartbeat_at: str
    expires_at: str
    ttl_seconds: int

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ProcessCompatibilityDeclaration":
        return cls(
            process_id=_normalize_text(data.get("process_id")),
            role=_normalize_text(data.get("role")) or "unknown",
            pid=int(data.get("pid") or 0),
            process_start_time=float(data.get("process_start_time") or 0.0),
            build_id=_normalize_text(data.get("build_id")) or "unknown",
            unknown_build_reason=(
                _normalize_text(data.get("unknown_build_reason")) or None
            ),
            writer_identity=_normalize_text(data.get("writer_identity")),
            supported_control_plane_api_version=_normalize_text(
                data.get("supported_control_plane_api_version")
            )
            or "unknown",
            max_supported_schema_generation=int(
                data.get("max_supported_schema_generation") or 0
            ),
            observed_schema_generation=int(data.get("observed_schema_generation") or 0),
            heartbeat_at=_normalize_text(data.get("heartbeat_at")),
            expires_at=_normalize_text(data.get("expires_at")),
            ttl_seconds=int(data.get("ttl_seconds") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "role": self.role,
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "build_id": self.build_id,
            "unknown_build_reason": self.unknown_build_reason,
            "writer_identity": self.writer_identity,
            "supported_control_plane_api_version": (
                self.supported_control_plane_api_version
            ),
            "max_supported_schema_generation": self.max_supported_schema_generation,
            "observed_schema_generation": self.observed_schema_generation,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
        }


@dataclass(frozen=True)
class CompatibilityEvaluation:
    status: CompatibilityStatus
    observed_schema: int
    supported_schema: int
    process_role: str
    build_id: str
    restart_required: bool
    reason: Optional[str] = None
    process_id: Optional[str] = None

    @property
    def compatible(self) -> bool:
        return self.status == "compatible"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_schema": self.observed_schema,
            "supported_schema": self.supported_schema,
            "process_role": self.process_role,
            "build_id": self.build_id,
            "restart_required": self.restart_required,
            "reason": self.reason,
            "process_id": self.process_id,
        }


@dataclass(frozen=True)
class CompatibilityRegistry:
    declarations: tuple[ProcessCompatibilityDeclaration, ...] = field(
        default_factory=tuple
    )
    stale_declarations: tuple[ProcessCompatibilityDeclaration, ...] = field(
        default_factory=tuple
    )
    updated_at: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CompatibilityRegistry":
        declarations = tuple(
            ProcessCompatibilityDeclaration.from_mapping(item)
            for item in data.get("declarations", [])
            if isinstance(item, Mapping)
        )
        stale_declarations = tuple(
            ProcessCompatibilityDeclaration.from_mapping(item)
            for item in data.get("stale_declarations", [])
            if isinstance(item, Mapping)
        )
        return cls(
            declarations=declarations,
            stale_declarations=stale_declarations,
            updated_at=_normalize_text(data.get("updated_at")) or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "declarations": [item.to_dict() for item in self.declarations],
            "stale_declarations": [item.to_dict() for item in self.stale_declarations],
        }


def _parse_utc_timestamp(value: str) -> Optional[float]:
    raw = _normalize_text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify_registry_declarations(
    declarations: tuple[ProcessCompatibilityDeclaration, ...],
    *,
    now_timestamp: float,
    pid_start_time_matches: Callable[[int, float], bool] | None = None,
) -> tuple[
    tuple[ProcessCompatibilityDeclaration, ...],
    tuple[ProcessCompatibilityDeclaration, ...],
]:
    active: list[ProcessCompatibilityDeclaration] = []
    stale: list[ProcessCompatibilityDeclaration] = []
    for declaration in declarations:
        expires_at = _parse_utc_timestamp(declaration.expires_at)
        pid_matches = (
            True
            if pid_start_time_matches is None
            else pid_start_time_matches(declaration.pid, declaration.process_start_time)
        )
        if expires_at is None or expires_at <= now_timestamp or not pid_matches:
            stale.append(declaration)
            continue
        active.append(declaration)
    return tuple(active), tuple(stale)


class SchemaCompatibilityError(RuntimeError):
    def __init__(self, evaluation: CompatibilityEvaluation) -> None:
        super().__init__(
            "orchestration.sqlite3 schema is newer than this build supports"
        )
        self.evaluation = evaluation


def evaluate_schema_compatibility(
    *,
    observed_schema: int,
    supported_schema: int,
    process_role: str,
    build_id: str,
    process_id: Optional[str] = None,
) -> CompatibilityEvaluation:
    observed = max(0, int(observed_schema))
    supported = max(0, int(supported_schema))
    if observed > supported:
        return CompatibilityEvaluation(
            status="incompatible_schema",
            observed_schema=observed,
            supported_schema=supported,
            process_role=process_role,
            build_id=build_id,
            restart_required=True,
            reason="observed schema generation is newer than this process supports",
            process_id=process_id,
        )
    return CompatibilityEvaluation(
        status="compatible",
        observed_schema=observed,
        supported_schema=supported,
        process_role=process_role,
        build_id=build_id,
        restart_required=False,
        process_id=process_id,
    )


def build_process_declaration(
    *,
    role: str,
    supported_control_plane_api_version: str,
    max_supported_schema_generation: int,
    observed_schema_generation: int,
    ttl_seconds: int = 120,
) -> ProcessCompatibilityDeclaration:
    build_id, unknown_reason = resolve_build_identity()
    pid = os.getpid()
    process_id = f"{role}:{pid}:{uuid.uuid4()}"
    heartbeat_at = now_iso()
    expires_at = datetime.fromtimestamp(
        time.time() + max(1, int(ttl_seconds)), tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ProcessCompatibilityDeclaration(
        process_id=process_id,
        role=role,
        pid=pid,
        process_start_time=time.time(),
        build_id=build_id,
        unknown_build_reason=unknown_reason,
        writer_identity=f"{socket.gethostname()}:{pid}:{process_id}",
        supported_control_plane_api_version=supported_control_plane_api_version,
        max_supported_schema_generation=max(0, int(max_supported_schema_generation)),
        observed_schema_generation=max(0, int(observed_schema_generation)),
        heartbeat_at=heartbeat_at,
        expires_at=expires_at,
        ttl_seconds=max(1, int(ttl_seconds)),
    )


__all__ = [
    "CompatibilityEvaluation",
    "CompatibilityRegistry",
    "CompatibilityStatus",
    "ProcessCompatibilityDeclaration",
    "ProcessRole",
    "SchemaCompatibilityError",
    "build_process_declaration",
    "classify_registry_declarations",
    "evaluate_schema_compatibility",
    "resolve_build_identity",
]
