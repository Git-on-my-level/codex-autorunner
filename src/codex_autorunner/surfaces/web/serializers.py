"""
Focused serializer models for web response payloads.

These models tighten the contracts for response payloads that were previously
Dict[str, Any] in schemas.py. Route modules and services should prefer these
typed shapes when assembling response data so that the public web contract
remains explicit and auditable.

Compatibility note: some response fields (corruption notices, upstream rate
limits) remain Dict[str, Any] because their shape is externally determined
and cannot be meaningfully narrowed without coupling to upstream contracts.

Serialization note: these models use exclude_none serialization so that
optional fields that are None are omitted from JSON output, matching the
previous Dict[str, Any] pass-through behavior.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, model_serializer


class _ExcludeNoneModel(BaseModel):
    """Base model that omits None-valued fields from serialized output."""

    model_config = ConfigDict(extra="ignore")

    @model_serializer(mode="wrap")
    def _exclude_none_serialization(self, handler: Any) -> Dict[str, Any]:
        result = handler(self)
        return {k: v for k, v in result.items() if v is not None}


class WorkspaceDestinationPayload(_ExcludeNoneModel):
    """Tagged union over local/docker workspace destinations.

    Serializes without None-valued fields so that a local destination
    produces ``{"kind": "local"}`` and a docker destination only includes
    the fields that are actually set.
    """

    kind: Literal["local", "docker"]
    image: Optional[str] = None
    container_name: Optional[str] = None
    workdir: Optional[str] = None
    profile: Optional[str] = None
    env_passthrough: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    mounts: Optional[List[Dict[str, Any]]] = None


class SectionFreshnessSummary(_ExcludeNoneModel):
    """Freshness summary for a single hub-message section (e.g. inbox)."""

    generated_at: str
    stale_threshold_seconds: int
    entity_count: int
    stale_count: int
    fresh_count: int
    unknown_count: int
    is_stale: bool
    partially_stale: bool
    oldest_basis_at: Optional[str] = None
    newest_basis_at: Optional[str] = None


class OrchestrationHealthPayload(BaseModel):
    """Orchestration sub-object for SystemHealthResponse.

    Uses ``extra="allow"`` so that ``last_housekeeping`` and any future
    fields pass through without being dropped by the response model.
    """

    model_config = ConfigDict(extra="allow")

    database_path: str
    database_size_bytes: int
    database_size_status: str
    database_size_warning_bytes: int
    database_size_error_bytes: int
    last_housekeeping: Optional[Dict[str, Any]] = None

    @model_serializer(mode="wrap")
    def _exclude_none_serialization(self, handler: Any) -> Dict[str, Any]:
        result = handler(self)
        return {k: v for k, v in result.items() if v is not None}


def build_orchestration_health(
    database_health: Any,
    last_housekeeping: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the orchestration health dict from a database health snapshot.

    This replaces inline dict construction in the system health route so the
    contract is owned by a single serializer function.
    """
    payload = OrchestrationHealthPayload(
        database_path=database_health.database_path,
        database_size_bytes=database_health.size_bytes,
        database_size_status=database_health.status,
        database_size_warning_bytes=database_health.warning_threshold_bytes,
        database_size_error_bytes=database_health.error_threshold_bytes,
        last_housekeeping=last_housekeeping,
    )
    return payload.model_dump(exclude_none=True)
