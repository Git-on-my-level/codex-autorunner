from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..time_utils import now_iso
from .sqlite import open_orchestration_sqlite

_SCALAR_TYPES = (str, int, float, bool)
_EXCLUDED_METADATA_KEYS = {
    "attachment_bodies",
    "attachments",
    "event_stream",
    "event_streams",
    "media",
    "media_bodies",
    "native_events",
    "raw_event",
    "raw_events",
    "raw_response",
    "reasoning_trace",
    "reasoning_traces",
    "stderr",
    "stdout",
    "tool_call",
    "tool_calls",
    "tool_payload",
    "tool_payloads",
    "tool_result",
    "tool_results",
}
_MAX_STRING_LENGTH = 16_000
_TRANSCRIPT_PREVIEW_CHARS = 400


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _truncate_text(value: str) -> str:
    if len(value) <= _MAX_STRING_LENGTH:
        return value
    return value[:_MAX_STRING_LENGTH]


def _sanitize_metadata_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, _SCALAR_TYPES):
        if isinstance(value, str):
            return _truncate_text(value)
        return value
    if depth >= 4:
        return None
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, inner in value.items():
            key_text = str(key)
            if key_text in _EXCLUDED_METADATA_KEYS:
                continue
            sanitized = _sanitize_metadata_value(inner, depth=depth + 1)
            if sanitized is not None:
                cleaned[key_text] = sanitized
        return cleaned
    if isinstance(value, (list, tuple)):
        cleaned_items = [
            _sanitize_metadata_value(item, depth=depth + 1) for item in value[:50]
        ]
        return [item for item in cleaned_items if item is not None]
    return None


def sanitize_transcript_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = _sanitize_metadata_value(dict(metadata), depth=0)
    if isinstance(cleaned, dict):
        return cleaned
    return {}


def build_text_preview(text_content: str) -> str:
    stripped = (text_content or "").strip()
    if len(stripped) <= _TRANSCRIPT_PREVIEW_CHARS:
        return stripped
    return stripped[:_TRANSCRIPT_PREVIEW_CHARS].rstrip() + "..."


def _normalize_target(metadata: Mapping[str, Any], *, turn_id: str) -> tuple[str, str]:
    managed_thread_id = str(metadata.get("managed_thread_id") or "").strip()
    if managed_thread_id:
        return "thread_target", managed_thread_id
    thread_id = str(metadata.get("thread_id") or "").strip()
    if thread_id:
        return "thread_target", thread_id
    lane_id = str(metadata.get("lane_id") or "").strip()
    if lane_id:
        return "lane", lane_id
    return "transcript_turn", turn_id


def _normalize_created_at(metadata: Mapping[str, Any]) -> str:
    for key in ("created_at", "finished_at", "event_timestamp", "started_at"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return now_iso()


@dataclass(frozen=True)
class TranscriptMirrorRow:
    transcript_mirror_id: str
    metadata: dict[str, Any]
    content: str
    preview: str

    def as_history_entry(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        payload.setdefault("turn_id", self.transcript_mirror_id)
        payload["preview"] = self.preview
        return payload

    def as_transcript(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "content": self.content,
        }


class TranscriptMirrorStore:
    def __init__(self, hub_root) -> None:
        self._hub_root = hub_root

    def write_mirror(
        self,
        *,
        turn_id: str,
        metadata: Mapping[str, Any],
        assistant_text: str,
    ) -> None:
        sanitized = sanitize_transcript_metadata(metadata)
        sanitized.setdefault("turn_id", turn_id)
        created_at = _normalize_created_at(sanitized)
        sanitized.setdefault("created_at", created_at)
        target_kind, target_id = _normalize_target(sanitized, turn_id=turn_id)
        execution_id = str(
            sanitized.get("managed_turn_id") or sanitized.get("turn_id") or turn_id
        ).strip()
        agent_id = str(sanitized.get("agent") or "").strip() or None
        model_id = str(sanitized.get("model") or "").strip() or None
        repo_id = str(sanitized.get("repo_id") or "").strip() or None
        text_content = assistant_text or ""
        text_preview = build_text_preview(text_content)
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute(
                """
                INSERT INTO orch_transcript_mirrors (
                    transcript_mirror_id,
                    target_kind,
                    target_id,
                    execution_id,
                    message_role,
                    text_content,
                    text_preview,
                    repo_id,
                    agent_id,
                    model_id,
                    created_at,
                    updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transcript_mirror_id) DO UPDATE SET
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    execution_id = excluded.execution_id,
                    message_role = excluded.message_role,
                    text_content = excluded.text_content,
                    text_preview = excluded.text_preview,
                    repo_id = excluded.repo_id,
                    agent_id = excluded.agent_id,
                    model_id = excluded.model_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    turn_id,
                    target_kind,
                    target_id,
                    execution_id or None,
                    "assistant",
                    text_content,
                    text_preview,
                    repo_id,
                    agent_id,
                    model_id,
                    created_at,
                    created_at,
                    _json_dumps(sanitized),
                ),
            )

    def read_transcript(self, turn_id: str) -> dict[str, Any] | None:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT transcript_mirror_id, metadata_json, text_content, text_preview
                  FROM orch_transcript_mirrors
                 WHERE transcript_mirror_id = ?
                """,
                (turn_id,),
            ).fetchone()
        parsed = self._row_to_record(row)
        if parsed is None:
            return None
        return parsed.as_transcript()

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with open_orchestration_sqlite(self._hub_root) as conn:
            rows = conn.execute(
                """
                SELECT transcript_mirror_id, metadata_json, text_content, text_preview
                  FROM orch_transcript_mirrors
                 ORDER BY rowid DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            parsed = self._row_to_record(row)
            if parsed is not None:
                entries.append(parsed.as_history_entry())
        return entries

    @staticmethod
    def _row_to_record(row: Any) -> TranscriptMirrorRow | None:
        if row is None:
            return None
        try:
            metadata_payload = json.loads(str(row["metadata_json"] or "{}"))
        except Exception:
            metadata_payload = {}
        metadata = dict(metadata_payload) if isinstance(metadata_payload, dict) else {}
        transcript_mirror_id = str(row["transcript_mirror_id"] or "").strip()
        if not transcript_mirror_id:
            return None
        metadata.setdefault("turn_id", transcript_mirror_id)
        content = str(row["text_content"] or "")
        preview = str(row["text_preview"] or "")
        if not preview:
            preview = build_text_preview(content)
        return TranscriptMirrorRow(
            transcript_mirror_id=transcript_mirror_id,
            metadata=metadata,
            content=content,
            preview=preview,
        )


__all__ = [
    "TranscriptMirrorStore",
    "TranscriptMirrorRow",
    "build_text_preview",
    "sanitize_transcript_metadata",
]
