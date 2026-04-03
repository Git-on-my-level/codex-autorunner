from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .pr_bindings import PrBinding
from .scm_events import ScmEvent
from .scm_reaction_types import ReactionIntent, ReactionKind
from .time_utils import now_iso

_RESOLVED_STATES_ALLOW_REEMIT = frozenset({"resolved"})


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_int(value: Any, *, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be a JSON object")


def _normalize_limit(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _json_dumps(payload: Mapping[str, Any]) -> str:
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


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(str(item) for item in value.keys()):
            child = _canonicalize(value.get(key))
            if child is None:
                continue
            if isinstance(child, (dict, list)) and not child:
                continue
            normalized[key] = child
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized_items = []
        for item in value:
            normalized = _canonicalize(item)
            if normalized is None:
                continue
            normalized_items.append(normalized)
        return normalized_items
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        normalized = _normalize_text(value)
        if normalized is not None:
            return normalized
    return None


def _resolved_repo_slug(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[str]:
    return _first_text(
        event.repo_slug, binding.repo_slug if binding is not None else None
    )


def _resolved_repo_id(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[str]:
    return _first_text(event.repo_id, binding.repo_id if binding is not None else None)


def _resolved_pr_number(event: ScmEvent, binding: Optional[PrBinding]) -> Optional[int]:
    return binding.pr_number if binding is not None else event.pr_number


def _target_fingerprint_payload(
    intent: ReactionIntent,
    *,
    binding: Optional[PrBinding],
    repo_id: Optional[str],
) -> dict[str, Any]:
    payload = intent.payload if isinstance(intent.payload, Mapping) else {}
    request = payload.get("request")
    request_mapping = request if isinstance(request, Mapping) else {}
    return {
        "delivery": _normalize_text(payload.get("delivery")),
        "repo_id": _first_text(payload.get("repo_id"), repo_id),
        "request_kind": _normalize_text(request_mapping.get("kind")),
        "thread_target_id": _first_text(
            payload.get("thread_target_id"),
            binding.thread_target_id if binding is not None else None,
        ),
    }


def _condition_fingerprint_payload(
    event: ScmEvent,
    *,
    intent: ReactionIntent,
) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, Mapping) else {}

    if intent.reaction_kind == "ci_failed":
        return {
            "check_name": _first_text(payload.get("name"), payload.get("check_name")),
            "conclusion": _normalize_text(payload.get("conclusion")),
            "external_id": _normalize_text(payload.get("external_id")),
            "head_sha": _normalize_text(payload.get("head_sha")),
            "status": _normalize_text(payload.get("status")),
        }
    if intent.reaction_kind in {"changes_requested", "approved_and_green"}:
        return {
            "author_login": _normalize_text(payload.get("author_login")),
            "body": _normalize_text(payload.get("body")),
            "review_id": _normalize_text(payload.get("review_id")),
            "review_state": _normalize_text(payload.get("review_state")),
        }
    if intent.reaction_kind == "review_comment":
        return {
            "author_login": _normalize_text(payload.get("author_login")),
            "author_type": _normalize_text(payload.get("author_type")),
            "body": _normalize_text(payload.get("body")),
            "comment_id": _normalize_text(payload.get("comment_id")),
            "html_url": _normalize_text(payload.get("html_url")),
            "issue_author_login": _normalize_text(payload.get("issue_author_login")),
            "line": (
                payload.get("line") if isinstance(payload.get("line"), int) else None
            ),
            "path": _normalize_text(payload.get("path")),
            "review_id": _normalize_text(payload.get("review_id")),
            "review_state": _normalize_text(payload.get("review_state")),
        }
    if intent.reaction_kind == "merged":
        return {
            "head_sha": _normalize_text(payload.get("head_sha")),
            "merge_commit_sha": _normalize_text(payload.get("merge_commit_sha")),
            "merged": (
                payload.get("merged")
                if isinstance(payload.get("merged"), bool)
                else None
            ),
            "state": _normalize_text(payload.get("state")),
        }
    return {}


def stable_reaction_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = _canonicalize(payload)
    if not isinstance(canonical, dict):
        canonical = {"value": canonical}
    encoded = _json_dumps(canonical)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def compute_reaction_fingerprint(
    event: ScmEvent,
    *,
    binding: Optional[PrBinding],
    intent: ReactionIntent,
) -> str:
    """Return a deterministic semantic fingerprint for one routed SCM reaction.

    The fingerprint intentionally excludes delivery-specific identifiers such as
    `event_id`, `delivery_id`, `received_at`, and `operation_key`. It instead
    hashes the stable reaction condition plus the reaction target so repeated
    webhook deliveries or repeated SCM events for the same unresolved condition
    collapse to the same fingerprint.
    """

    repo_id = _resolved_repo_id(event, binding)
    fingerprint_payload = {
        "condition": _condition_fingerprint_payload(event, intent=intent),
        "event_type": event.event_type,
        "operation_kind": intent.operation_kind,
        "provider": event.provider,
        "pr_number": _resolved_pr_number(event, binding),
        "reaction_kind": intent.reaction_kind,
        "repo_id": repo_id,
        "repo_slug": _resolved_repo_slug(event, binding),
        "target": _target_fingerprint_payload(intent, binding=binding, repo_id=repo_id),
    }
    return stable_reaction_fingerprint(fingerprint_payload)


@dataclass(frozen=True)
class ScmReactionState:
    binding_id: str
    reaction_kind: str
    fingerprint: str
    state: str
    created_at: str
    updated_at: str
    first_event_id: Optional[str] = None
    last_event_id: Optional[str] = None
    last_operation_key: Optional[str] = None
    first_emitted_at: Optional[str] = None
    last_emitted_at: Optional[str] = None
    last_delivery_failed_at: Optional[str] = None
    escalated_at: Optional[str] = None
    resolved_at: Optional[str] = None
    attempt_count: int = 0
    delivery_failure_count: int = 0
    last_error_text: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _state_from_row(row: sqlite3.Row) -> ScmReactionState:
    return ScmReactionState(
        binding_id=str(row["binding_id"]),
        reaction_kind=str(row["reaction_kind"]),
        fingerprint=str(row["fingerprint"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        first_event_id=_normalize_text(row["first_event_id"]),
        last_event_id=_normalize_text(row["last_event_id"]),
        last_operation_key=_normalize_text(row["last_operation_key"]),
        first_emitted_at=_normalize_text(row["first_emitted_at"]),
        last_emitted_at=_normalize_text(row["last_emitted_at"]),
        last_delivery_failed_at=_normalize_text(row["last_delivery_failed_at"]),
        escalated_at=_normalize_text(row["escalated_at"]),
        resolved_at=_normalize_text(row["resolved_at"]),
        attempt_count=_normalize_int(
            row["attempt_count"] or 0, field_name="attempt_count"
        ),
        delivery_failure_count=_normalize_int(
            row["delivery_failure_count"] or 0,
            field_name="delivery_failure_count",
        ),
        last_error_text=_normalize_text(row["last_error_text"]),
        metadata=_json_loads_object(row["metadata_json"]),
    )


class ScmReactionStateStore:
    """SQLite-backed durable reaction state keyed by binding, kind, and fingerprint."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def compute_reaction_fingerprint(
        self,
        event: ScmEvent,
        *,
        binding: Optional[PrBinding],
        intent: ReactionIntent,
    ) -> str:
        return compute_reaction_fingerprint(event, binding=binding, intent=intent)

    def get_reaction_state(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
    ) -> Optional[ScmReactionState]:
        normalized_binding_id = _normalize_text(binding_id)
        normalized_reaction_kind = _normalize_text(reaction_kind)
        normalized_fingerprint = _normalize_text(fingerprint)
        if (
            normalized_binding_id is None
            or normalized_reaction_kind is None
            or normalized_fingerprint is None
        ):
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        return _state_from_row(row) if row is not None else None

    def list_reaction_states(
        self,
        *,
        binding_id: Optional[str] = None,
        reaction_kind: Optional[ReactionKind | str] = None,
        state: Optional[str] = None,
        last_event_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[ScmReactionState]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        where_clauses = ["1 = 1"]
        params: list[Any] = []

        normalized_binding_id = _normalize_text(binding_id)
        if normalized_binding_id is not None:
            where_clauses.append("binding_id = ?")
            params.append(normalized_binding_id)

        normalized_reaction_kind = _normalize_text(reaction_kind)
        if normalized_reaction_kind is not None:
            where_clauses.append("reaction_kind = ?")
            params.append(normalized_reaction_kind)

        normalized_state = _normalize_text(state)
        if normalized_state is not None:
            where_clauses.append("state = ?")
            params.append(normalized_state)

        normalized_last_event_id = _normalize_text(last_event_id)
        if normalized_last_event_id is not None:
            where_clauses.append("last_event_id = ?")
            params.append(normalized_last_event_id)

        params.append(resolved_limit)

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_reaction_state
                 WHERE {' AND '.join(where_clauses)}
                 ORDER BY updated_at DESC,
                          created_at DESC,
                          binding_id DESC,
                          reaction_kind DESC,
                          fingerprint DESC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_state_from_row(row) for row in rows]

    def should_emit_reaction(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
    ) -> bool:
        existing = self.get_reaction_state(
            binding_id=binding_id,
            reaction_kind=reaction_kind,
            fingerprint=fingerprint,
        )
        if existing is None:
            return True
        return existing.state in _RESOLVED_STATES_ALLOW_REEMIT

    def mark_reaction_emitted(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        normalized_operation_key = _normalize_text(operation_key)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                conn.execute(
                    """
                    INSERT INTO orch_reaction_state (
                        binding_id,
                        reaction_kind,
                        fingerprint,
                        state,
                        first_event_id,
                        last_event_id,
                        last_operation_key,
                        created_at,
                        updated_at,
                        first_emitted_at,
                        last_emitted_at,
                        last_delivery_failed_at,
                        escalated_at,
                        resolved_at,
                        attempt_count,
                        delivery_failure_count,
                        last_error_text,
                        metadata_json
                    ) VALUES (?, ?, ?, 'emitted', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1, 0, NULL, ?)
                    """,
                    (
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                        normalized_event_id,
                        normalized_event_id,
                        normalized_operation_key,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                        _json_dumps(metadata_object),
                    ),
                )
            else:
                merged_metadata = self._merge_metadata(row, metadata_object)
                conn.execute(
                    """
                    UPDATE orch_reaction_state
                       SET state = 'emitted',
                           first_event_id = COALESCE(first_event_id, ?),
                           last_event_id = ?,
                           last_operation_key = COALESCE(?, last_operation_key),
                           updated_at = ?,
                           first_emitted_at = COALESCE(first_emitted_at, ?),
                           last_emitted_at = ?,
                           escalated_at = NULL,
                           resolved_at = NULL,
                           attempt_count = attempt_count + 1,
                           last_error_text = NULL,
                           metadata_json = ?
                     WHERE binding_id = ?
                       AND reaction_kind = ?
                       AND fingerprint = ?
                    """,
                    (
                        normalized_event_id,
                        normalized_event_id,
                        normalized_operation_key,
                        timestamp,
                        timestamp,
                        timestamp,
                        _json_dumps(merged_metadata),
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                    ),
                )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError("reaction state row missing after mark_reaction_emitted")
        return _state_from_row(refreshed)

    def mark_reaction_delivery_failed(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        error_text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        normalized_error_text = _normalize_text(error_text)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                conn.execute(
                    """
                    INSERT INTO orch_reaction_state (
                        binding_id,
                        reaction_kind,
                        fingerprint,
                        state,
                        first_event_id,
                        last_event_id,
                        last_operation_key,
                        created_at,
                        updated_at,
                        first_emitted_at,
                        last_emitted_at,
                        last_delivery_failed_at,
                        escalated_at,
                        resolved_at,
                        attempt_count,
                        delivery_failure_count,
                        last_error_text,
                        metadata_json
                    ) VALUES (?, ?, ?, 'delivery_failed', ?, ?, NULL, ?, ?, NULL, NULL, ?, NULL, NULL, 0, 1, ?, ?)
                    """,
                    (
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                        normalized_event_id,
                        normalized_event_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        normalized_error_text,
                        _json_dumps(metadata_object),
                    ),
                )
            else:
                merged_metadata = self._merge_metadata(row, metadata_object)
                conn.execute(
                    """
                    UPDATE orch_reaction_state
                       SET state = 'delivery_failed',
                           first_event_id = COALESCE(first_event_id, ?),
                           last_event_id = ?,
                           updated_at = ?,
                           last_delivery_failed_at = ?,
                           delivery_failure_count = delivery_failure_count + 1,
                           last_error_text = ?,
                           metadata_json = ?
                     WHERE binding_id = ?
                       AND reaction_kind = ?
                       AND fingerprint = ?
                    """,
                    (
                        normalized_event_id,
                        normalized_event_id,
                        timestamp,
                        timestamp,
                        normalized_error_text,
                        _json_dumps(merged_metadata),
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                    ),
                )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError(
                "reaction state row missing after mark_reaction_delivery_failed"
            )
        return _state_from_row(refreshed)

    def mark_reaction_delivery_succeeded(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        normalized_operation_key = _normalize_text(operation_key)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                raise RuntimeError("reaction state row missing before delivery success")
            merged_metadata = self._merge_metadata(row, metadata_object)
            conn.execute(
                """
                UPDATE orch_reaction_state
                   SET state = 'emitted',
                       last_event_id = COALESCE(?, last_event_id),
                       last_operation_key = COALESCE(?, last_operation_key),
                       updated_at = ?,
                       first_emitted_at = COALESCE(first_emitted_at, ?),
                       last_emitted_at = ?,
                       resolved_at = NULL,
                       last_error_text = NULL,
                       metadata_json = ?
                 WHERE binding_id = ?
                   AND reaction_kind = ?
                   AND fingerprint = ?
                """,
                (
                    normalized_event_id,
                    normalized_operation_key,
                    timestamp,
                    timestamp,
                    timestamp,
                    _json_dumps(merged_metadata),
                    normalized_binding_id,
                    normalized_reaction_kind,
                    normalized_fingerprint,
                ),
            )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError(
                "reaction state row missing after mark_reaction_delivery_succeeded"
            )
        return _state_from_row(refreshed)

    def mark_reaction_resolved(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                conn.execute(
                    """
                    INSERT INTO orch_reaction_state (
                        binding_id,
                        reaction_kind,
                        fingerprint,
                        state,
                        first_event_id,
                        last_event_id,
                        last_operation_key,
                        created_at,
                        updated_at,
                        first_emitted_at,
                        last_emitted_at,
                        last_delivery_failed_at,
                        escalated_at,
                        resolved_at,
                        attempt_count,
                        delivery_failure_count,
                        last_error_text,
                        metadata_json
                    ) VALUES (?, ?, ?, 'resolved', ?, ?, NULL, ?, ?, NULL, NULL, NULL, NULL, ?, 0, 0, NULL, ?)
                    """,
                    (
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                        normalized_event_id,
                        normalized_event_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        _json_dumps(metadata_object),
                    ),
                )
            else:
                merged_metadata = self._merge_metadata(row, metadata_object)
                conn.execute(
                    """
                    UPDATE orch_reaction_state
                       SET state = 'resolved',
                           first_event_id = COALESCE(first_event_id, ?),
                           last_event_id = ?,
                           updated_at = ?,
                           resolved_at = ?,
                           metadata_json = ?
                     WHERE binding_id = ?
                       AND reaction_kind = ?
                       AND fingerprint = ?
                    """,
                    (
                        normalized_event_id,
                        normalized_event_id,
                        timestamp,
                        timestamp,
                        _json_dumps(merged_metadata),
                        normalized_binding_id,
                        normalized_reaction_kind,
                        normalized_fingerprint,
                    ),
                )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError(
                "reaction state row missing after mark_reaction_resolved"
            )
        return _state_from_row(refreshed)

    def mark_reaction_suppressed(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                raise RuntimeError("reaction state row missing before suppression")
            merged_metadata = self._merge_metadata(row, metadata_object)
            conn.execute(
                """
                UPDATE orch_reaction_state
                   SET last_event_id = COALESCE(?, last_event_id),
                       updated_at = ?,
                       attempt_count = attempt_count + 1,
                       metadata_json = ?
                 WHERE binding_id = ?
                   AND reaction_kind = ?
                   AND fingerprint = ?
                """,
                (
                    normalized_event_id,
                    timestamp,
                    _json_dumps(merged_metadata),
                    normalized_binding_id,
                    normalized_reaction_kind,
                    normalized_fingerprint,
                ),
            )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError(
                "reaction state row missing after mark_reaction_suppressed"
            )
        return _state_from_row(refreshed)

    def mark_reaction_escalated(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
        event_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScmReactionState:
        normalized_binding_id, normalized_reaction_kind, normalized_fingerprint = (
            self._normalize_identity(
                binding_id=binding_id,
                reaction_kind=reaction_kind,
                fingerprint=fingerprint,
            )
        )
        normalized_event_id = _normalize_text(event_id)
        normalized_operation_key = _normalize_text(operation_key)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
            if row is None:
                raise RuntimeError("reaction state row missing before escalation")
            merged_metadata = self._merge_metadata(row, metadata_object)
            conn.execute(
                """
                UPDATE orch_reaction_state
                   SET last_event_id = COALESCE(?, last_event_id),
                       last_operation_key = COALESCE(?, last_operation_key),
                       updated_at = ?,
                       escalated_at = COALESCE(escalated_at, ?),
                       attempt_count = attempt_count + 1,
                       metadata_json = ?
                 WHERE binding_id = ?
                   AND reaction_kind = ?
                   AND fingerprint = ?
                """,
                (
                    normalized_event_id,
                    normalized_operation_key,
                    timestamp,
                    timestamp,
                    _json_dumps(merged_metadata),
                    normalized_binding_id,
                    normalized_reaction_kind,
                    normalized_fingerprint,
                ),
            )
            refreshed = self._load_reaction_state_row(
                conn,
                binding_id=normalized_binding_id,
                reaction_kind=normalized_reaction_kind,
                fingerprint=normalized_fingerprint,
            )
        if refreshed is None:
            raise RuntimeError(
                "reaction state row missing after mark_reaction_escalated"
            )
        return _state_from_row(refreshed)

    def resolve_other_active_reactions(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        keep_fingerprint: str,
        event_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        normalized_binding_id = _normalize_text(binding_id)
        normalized_reaction_kind = _normalize_text(reaction_kind)
        normalized_keep_fingerprint = _normalize_text(keep_fingerprint)
        if (
            normalized_binding_id is None
            or normalized_reaction_kind is None
            or normalized_keep_fingerprint is None
        ):
            return 0
        normalized_event_id = _normalize_text(event_id)
        metadata_object = _normalize_json_object(metadata, field_name="metadata")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                """
                SELECT fingerprint, metadata_json
                  FROM orch_reaction_state
                 WHERE binding_id = ?
                   AND reaction_kind = ?
                   AND fingerprint != ?
                   AND state IN ('delivery_failed', 'emitted')
                """,
                (
                    normalized_binding_id,
                    normalized_reaction_kind,
                    normalized_keep_fingerprint,
                ),
            ).fetchall()
            resolved = 0
            for row in rows:
                fingerprint = _normalize_text(row["fingerprint"])
                if fingerprint is None:
                    continue
                merged_metadata = _json_loads_object(row["metadata_json"])
                merged_metadata.update(metadata_object)
                cursor = conn.execute(
                    """
                    UPDATE orch_reaction_state
                       SET state = 'resolved',
                           last_event_id = COALESCE(?, last_event_id),
                           updated_at = ?,
                           resolved_at = ?,
                           metadata_json = ?
                     WHERE binding_id = ?
                       AND reaction_kind = ?
                       AND fingerprint = ?
                       AND state IN ('delivery_failed', 'emitted')
                    """,
                    (
                        normalized_event_id,
                        timestamp,
                        timestamp,
                        _json_dumps(merged_metadata),
                        normalized_binding_id,
                        normalized_reaction_kind,
                        fingerprint,
                    ),
                )
                resolved += int(cursor.rowcount or 0)
        return resolved

    def _normalize_identity(
        self,
        *,
        binding_id: str,
        reaction_kind: ReactionKind | str,
        fingerprint: str,
    ) -> tuple[str, str, str]:
        normalized_binding_id = _normalize_text(binding_id)
        normalized_reaction_kind = _normalize_text(reaction_kind)
        normalized_fingerprint = _normalize_text(fingerprint)
        if normalized_binding_id is None:
            raise ValueError("binding_id is required")
        if normalized_reaction_kind is None:
            raise ValueError("reaction_kind is required")
        if normalized_fingerprint is None:
            raise ValueError("fingerprint is required")
        return (
            normalized_binding_id,
            normalized_reaction_kind,
            normalized_fingerprint,
        )

    def _load_reaction_state_row(
        self,
        conn: sqlite3.Connection,
        *,
        binding_id: str,
        reaction_kind: str,
        fingerprint: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_reaction_state
             WHERE binding_id = ?
               AND reaction_kind = ?
               AND fingerprint = ?
            """,
            (binding_id, reaction_kind, fingerprint),
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def _merge_metadata(
        self,
        row: sqlite3.Row,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        merged = _json_loads_object(row["metadata_json"])
        merged.update(metadata)
        return merged


def get_reaction_state(
    hub_root: Path,
    *,
    binding_id: str,
    reaction_kind: ReactionKind | str,
    fingerprint: str,
) -> Optional[ScmReactionState]:
    return ScmReactionStateStore(hub_root).get_reaction_state(
        binding_id=binding_id,
        reaction_kind=reaction_kind,
        fingerprint=fingerprint,
    )


def list_reaction_states(hub_root: Path, **kwargs: Any) -> list[ScmReactionState]:
    return ScmReactionStateStore(hub_root).list_reaction_states(**kwargs)


def should_emit_reaction(
    hub_root: Path,
    *,
    binding_id: str,
    reaction_kind: ReactionKind | str,
    fingerprint: str,
) -> bool:
    return ScmReactionStateStore(hub_root).should_emit_reaction(
        binding_id=binding_id,
        reaction_kind=reaction_kind,
        fingerprint=fingerprint,
    )


def mark_reaction_emitted(hub_root: Path, **kwargs: Any) -> ScmReactionState:
    return ScmReactionStateStore(hub_root).mark_reaction_emitted(**kwargs)


def mark_reaction_delivery_failed(hub_root: Path, **kwargs: Any) -> ScmReactionState:
    return ScmReactionStateStore(hub_root).mark_reaction_delivery_failed(**kwargs)


def mark_reaction_resolved(hub_root: Path, **kwargs: Any) -> ScmReactionState:
    return ScmReactionStateStore(hub_root).mark_reaction_resolved(**kwargs)


__all__ = [
    "ScmReactionState",
    "ScmReactionStateStore",
    "compute_reaction_fingerprint",
    "get_reaction_state",
    "list_reaction_states",
    "mark_reaction_delivery_failed",
    "mark_reaction_emitted",
    "mark_reaction_resolved",
    "should_emit_reaction",
    "stable_reaction_fingerprint",
]
