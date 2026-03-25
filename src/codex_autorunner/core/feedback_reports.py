from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .time_utils import now_iso


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def _normalize_title(value: Any, *, field_name: str) -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return " ".join(normalized.split())


def _normalize_body(value: Any, *, field_name: str) -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def _normalize_limit(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_confidence(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number") from exc
    if not math.isfinite(normalized):
        raise ValueError("confidence must be finite")
    if normalized < 0 or normalized > 1:
        raise ValueError("confidence must be between 0 and 1")
    return round(normalized, 6)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _canonicalize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in sorted(
            (
                (str(item), _canonicalize_json(item_value))
                for item, item_value in value.items()
            ),
            key=lambda item: item[0],
        ):
            if child is None:
                continue
            if isinstance(child, (dict, list)) and not child:
                continue
            normalized[key] = child
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized_items = []
        for item in value:
            child = _canonicalize_json(item)
            if child is None:
                continue
            if isinstance(child, (dict, list)) and not child:
                continue
            normalized_items.append(child)
        normalized_items.sort(key=_json_dumps)
        return normalized_items
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence values must be finite")
        return round(value, 6)
    if value is None:
        return None
    raise ValueError("evidence must contain JSON-compatible values")


def _normalize_evidence(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("evidence must be a JSON array")
    normalized = _canonicalize_json(list(value))
    return normalized if isinstance(normalized, list) else []


def _json_loads_array(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = _canonicalize_json(list(value))
        return normalized if isinstance(normalized, list) else []
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return _normalize_evidence(parsed)


@dataclass(frozen=True)
class NormalizedFeedbackReportInput:
    report_kind: str
    title: str
    body: str
    evidence: list[Any] = field(default_factory=list)
    confidence: Optional[float] = None
    source_kind: str = ""
    repo_id: Optional[str] = None
    thread_target_id: Optional[str] = None
    source_id: Optional[str] = None
    status: str = "open"
    report_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dedupe_key: str = ""

    def to_dedupe_payload(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "thread_target_id": self.thread_target_id,
            "report_kind": self.report_kind,
            "title": self.title,
            "body": self.body,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class FeedbackReport:
    report_id: str
    report_kind: str
    title: str
    body: str
    evidence: list[Any]
    source_kind: str
    dedupe_key: str
    status: str
    created_at: str
    updated_at: str
    repo_id: Optional[str] = None
    thread_target_id: Optional[str] = None
    confidence: Optional[float] = None
    source_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stable_feedback_report_dedupe_key(
    value: Mapping[str, Any] | NormalizedFeedbackReportInput,
) -> str:
    if isinstance(value, NormalizedFeedbackReportInput):
        payload = value.to_dedupe_payload()
    else:
        payload = dict(value)
    canonical = _canonicalize_json(payload)
    encoded = _json_dumps(
        canonical if isinstance(canonical, dict) else {"value": canonical}
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def normalize_feedback_report_input(
    *,
    report_kind: str,
    title: str,
    body: str,
    source_kind: str,
    repo_id: Optional[str] = None,
    thread_target_id: Optional[str] = None,
    evidence: Optional[Sequence[Any]] = None,
    confidence: Optional[float] = None,
    source_id: Optional[str] = None,
    status: str = "open",
    report_id: Optional[str] = None,
) -> NormalizedFeedbackReportInput:
    normalized_report_id = _normalize_text(report_id) or uuid.uuid4().hex
    normalized = NormalizedFeedbackReportInput(
        report_id=normalized_report_id,
        repo_id=_normalize_text(repo_id),
        thread_target_id=_normalize_text(thread_target_id),
        report_kind=_normalize_title(report_kind, field_name="report_kind"),
        title=_normalize_title(title, field_name="title"),
        body=_normalize_body(body, field_name="body"),
        evidence=_normalize_evidence(evidence),
        confidence=_normalize_confidence(confidence),
        source_kind=_normalize_title(source_kind, field_name="source_kind"),
        source_id=_normalize_text(source_id),
        status=_normalize_title(status, field_name="status"),
    )
    return NormalizedFeedbackReportInput(
        **{
            **asdict(normalized),
            "dedupe_key": stable_feedback_report_dedupe_key(normalized),
        }
    )


def _report_from_row(row: sqlite3.Row) -> FeedbackReport:
    confidence_value = row["confidence"]
    return FeedbackReport(
        report_id=str(row["report_id"]),
        repo_id=_normalize_text(row["repo_id"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        report_kind=str(row["report_kind"]),
        title=str(row["title"]),
        body=str(row["body"]),
        evidence=_json_loads_array(row["evidence_json"]),
        confidence=(
            _normalize_confidence(confidence_value)
            if confidence_value is not None
            else None
        ),
        source_kind=str(row["source_kind"]),
        source_id=_normalize_text(row["source_id"]),
        dedupe_key=str(row["dedupe_key"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class FeedbackReportStore:
    """SQLite-backed durable capture for structured feedback reports."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def create_report(self, **kwargs: Any) -> FeedbackReport:
        normalized = normalize_feedback_report_input(**kwargs)
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute(
                """
                INSERT INTO orch_feedback_reports (
                    report_id,
                    repo_id,
                    thread_target_id,
                    report_kind,
                    title,
                    body,
                    evidence_json,
                    confidence,
                    source_kind,
                    source_id,
                    dedupe_key,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.report_id,
                    normalized.repo_id,
                    normalized.thread_target_id,
                    normalized.report_kind,
                    normalized.title,
                    normalized.body,
                    _json_dumps(normalized.evidence),
                    normalized.confidence,
                    normalized.source_kind,
                    normalized.source_id,
                    normalized.dedupe_key,
                    normalized.status,
                    timestamp,
                    timestamp,
                ),
            )
            row = self._load_report_row(conn, report_id=normalized.report_id)
        if row is None:
            raise RuntimeError("feedback report row missing after insert")
        return _report_from_row(row)

    def get_report(self, report_id: str) -> Optional[FeedbackReport]:
        normalized_report_id = _normalize_text(report_id)
        if normalized_report_id is None:
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_report_row(conn, report_id=normalized_report_id)
        return _report_from_row(row) if row is not None else None

    def list_reports(
        self,
        *,
        repo_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        report_kind: Optional[str] = None,
        source_kind: Optional[str] = None,
        source_id: Optional[str] = None,
        status: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        limit: int = 50,
    ) -> list[FeedbackReport]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        where_clauses = ["1 = 1"]
        params: list[Any] = []

        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            where_clauses.append("repo_id = ?")
            params.append(normalized_repo_id)

        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_thread_target_id is not None:
            where_clauses.append("thread_target_id = ?")
            params.append(normalized_thread_target_id)

        normalized_report_kind = _normalize_text(report_kind)
        if normalized_report_kind is not None:
            where_clauses.append("report_kind = ?")
            params.append(" ".join(normalized_report_kind.split()))

        normalized_source_kind = _normalize_text(source_kind)
        if normalized_source_kind is not None:
            where_clauses.append("source_kind = ?")
            params.append(" ".join(normalized_source_kind.split()))

        normalized_source_id = _normalize_text(source_id)
        if normalized_source_id is not None:
            where_clauses.append("source_id = ?")
            params.append(normalized_source_id)

        normalized_status = _normalize_text(status)
        if normalized_status is not None:
            where_clauses.append("status = ?")
            params.append(" ".join(normalized_status.split()))

        normalized_dedupe_key = _normalize_text(dedupe_key)
        if normalized_dedupe_key is not None:
            where_clauses.append("dedupe_key = ?")
            params.append(normalized_dedupe_key)

        params.append(resolved_limit)

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_feedback_reports
                 WHERE {' AND '.join(where_clauses)}
                 ORDER BY updated_at DESC, created_at DESC, report_id DESC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_report_from_row(row) for row in rows]

    def _load_report_row(
        self,
        conn: sqlite3.Connection,
        *,
        report_id: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_feedback_reports
             WHERE report_id = ?
            """,
            (report_id,),
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None


def create_report(hub_root: Path, **kwargs: Any) -> FeedbackReport:
    return FeedbackReportStore(hub_root).create_report(**kwargs)


def get_report(hub_root: Path, report_id: str) -> Optional[FeedbackReport]:
    return FeedbackReportStore(hub_root).get_report(report_id)


def list_reports(hub_root: Path, **kwargs: Any) -> list[FeedbackReport]:
    return FeedbackReportStore(hub_root).list_reports(**kwargs)


__all__ = [
    "FeedbackReport",
    "FeedbackReportStore",
    "NormalizedFeedbackReportInput",
    "create_report",
    "get_report",
    "list_reports",
    "normalize_feedback_report_input",
    "stable_feedback_report_dedupe_key",
]
