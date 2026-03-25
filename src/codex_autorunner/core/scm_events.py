from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .time_utils import now_iso


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_int(value: Any, *, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _normalize_limit(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_timestamp(value: Any, *, field_name: str) -> Optional[str]:
    normalized = _normalize_text(value)
    if normalized is None:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return normalized


def _normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be a JSON object")


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _json_loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class ScmEvent:
    event_id: str
    provider: str
    event_type: str
    occurred_at: str
    received_at: str
    created_at: str
    repo_slug: Optional[str] = None
    repo_id: Optional[str] = None
    pr_number: Optional[int] = None
    delivery_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw_payload: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_from_row(row: Any) -> ScmEvent:
    return ScmEvent(
        event_id=str(row["event_id"]),
        provider=str(row["provider"]),
        event_type=str(row["event_type"]),
        occurred_at=str(row["occurred_at"]),
        received_at=str(row["received_at"]),
        created_at=str(row["created_at"]),
        repo_slug=_normalize_text(row["repo_slug"]),
        repo_id=_normalize_text(row["repo_id"]),
        pr_number=_normalize_int(row["pr_number"], field_name="pr_number"),
        delivery_id=_normalize_text(row["delivery_id"]),
        payload=_json_loads_object(row["payload_json"]),
        raw_payload=(
            _json_loads_object(row["raw_payload_json"])
            if _normalize_text(row["raw_payload_json"]) is not None
            else None
        ),
    )


class ScmEventStore:
    """SQLite-backed storage for canonical SCM events."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def record_event(
        self,
        *,
        provider: str,
        event_type: str,
        occurred_at: Optional[str] = None,
        received_at: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_number: Optional[int] = None,
        delivery_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        raw_payload: Optional[dict[str, Any]] = None,
        event_id: Optional[str] = None,
        max_raw_payload_bytes: int = 65_536,
    ) -> ScmEvent:
        normalized_provider = _normalize_text(provider)
        normalized_event_type = _normalize_text(event_type)
        normalized_event_id = _normalize_text(event_id) or uuid.uuid4().hex
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_event_type is None:
            raise ValueError("event_type is required")

        payload_object = _normalize_json_object(payload, field_name="payload")
        raw_payload_object = (
            _normalize_json_object(raw_payload, field_name="raw_payload")
            if raw_payload is not None
            else None
        )
        if max_raw_payload_bytes <= 0:
            raise ValueError("max_raw_payload_bytes must be > 0")
        raw_payload_json: Optional[str] = None
        if raw_payload_object is not None:
            raw_payload_json = _json_dumps(raw_payload_object)
            if len(raw_payload_json.encode("utf-8")) > max_raw_payload_bytes:
                raise ValueError("raw_payload exceeds max_raw_payload_bytes")

        resolved_received_at = (
            _normalize_timestamp(received_at, field_name="received_at") or now_iso()
        )
        resolved_occurred_at = (
            _normalize_timestamp(occurred_at, field_name="occurred_at")
            or resolved_received_at
        )
        created_at = now_iso()
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute(
                """
                INSERT INTO orch_scm_events (
                    event_id,
                    provider,
                    event_type,
                    repo_slug,
                    repo_id,
                    pr_number,
                    delivery_id,
                    occurred_at,
                    received_at,
                    payload_json,
                    raw_payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_id,
                    normalized_provider,
                    normalized_event_type,
                    _normalize_text(repo_slug),
                    _normalize_text(repo_id),
                    normalized_pr_number,
                    _normalize_text(delivery_id),
                    resolved_occurred_at,
                    resolved_received_at,
                    _json_dumps(payload_object),
                    raw_payload_json,
                    created_at,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_events
                 WHERE event_id = ?
                """,
                (normalized_event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("SCM event row missing after insert")
        return _event_from_row(row)

    def list_events(
        self,
        *,
        provider: Optional[str] = None,
        event_type: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_id: Optional[str] = None,
        pr_number: Optional[int] = None,
        delivery_id: Optional[str] = None,
        occurred_after: Optional[str] = None,
        occurred_before: Optional[str] = None,
        limit: int = 50,
    ) -> list[ScmEvent]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        where_clauses = ["1 = 1"]
        params: list[Any] = []

        normalized_provider = _normalize_text(provider)
        if normalized_provider is not None:
            where_clauses.append("provider = ?")
            params.append(normalized_provider)

        normalized_event_type = _normalize_text(event_type)
        if normalized_event_type is not None:
            where_clauses.append("event_type = ?")
            params.append(normalized_event_type)

        normalized_repo_slug = _normalize_text(repo_slug)
        if normalized_repo_slug is not None:
            where_clauses.append("repo_slug = ?")
            params.append(normalized_repo_slug)

        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            where_clauses.append("repo_id = ?")
            params.append(normalized_repo_id)

        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        if normalized_pr_number is not None:
            where_clauses.append("pr_number = ?")
            params.append(normalized_pr_number)

        normalized_delivery_id = _normalize_text(delivery_id)
        if normalized_delivery_id is not None:
            where_clauses.append("delivery_id = ?")
            params.append(normalized_delivery_id)

        normalized_occurred_after = _normalize_timestamp(
            occurred_after, field_name="occurred_after"
        )
        if normalized_occurred_after is not None:
            where_clauses.append("occurred_at >= ?")
            params.append(normalized_occurred_after)

        normalized_occurred_before = _normalize_timestamp(
            occurred_before, field_name="occurred_before"
        )
        if normalized_occurred_before is not None:
            where_clauses.append("occurred_at <= ?")
            params.append(normalized_occurred_before)

        params.append(resolved_limit)

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_scm_events
                 WHERE {' AND '.join(where_clauses)}
                 ORDER BY occurred_at DESC, created_at DESC, event_id DESC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_event_from_row(row) for row in rows]


def record_event(hub_root: Path, **kwargs: Any) -> ScmEvent:
    return ScmEventStore(hub_root).record_event(**kwargs)


def list_events(hub_root: Path, **kwargs: Any) -> list[ScmEvent]:
    return ScmEventStore(hub_root).list_events(**kwargs)


__all__ = [
    "ScmEvent",
    "ScmEventStore",
    "list_events",
    "record_event",
]
