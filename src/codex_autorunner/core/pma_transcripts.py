from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .orchestration.transcript_mirror import (
    TranscriptMirrorStore,
    build_plain_text_transcript,
)
from .redaction import redact_jsonable, redact_text
from .time_utils import now_iso
from .utils import atomic_write

logger = logging.getLogger(__name__)

PMA_TRANSCRIPTS_DIRNAME = "transcripts"
PMA_TRANSCRIPT_VERSION = 1
PMA_TRANSCRIPT_PREVIEW_CHARS = 400


def default_pma_transcripts_dir(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_TRANSCRIPTS_DIRNAME


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip())
    cleaned = cleaned.strip("-._")
    if not cleaned:
        return "unknown"
    return cleaned[:120]


def _stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class PmaTranscriptPointer:
    turn_id: str
    metadata_path: str
    content_path: str
    created_at: str


@dataclass(frozen=True)
class PmaTranscriptBackfillResult:
    imported_count: int
    skipped_count: int


class PmaTranscriptStore:
    def __init__(self, hub_root: Path) -> None:
        self._dir = default_pma_transcripts_dir(hub_root)
        self._mirror_store = TranscriptMirrorStore(hub_root)

    @property
    def dir(self) -> Path:
        return self._dir

    def write_transcript(
        self,
        *,
        turn_id: str,
        metadata: dict[str, Any],
        user_text: Optional[str] = None,
        assistant_text: str,
    ) -> PmaTranscriptPointer:
        safe_turn_id = _safe_segment(turn_id)
        stamp = _stamp_now()
        base = f"{stamp}_{safe_turn_id}"
        json_path = self._dir / f"{base}.json"
        md_path = self._dir / f"{base}.md"

        payload = dict(metadata)
        payload.setdefault("version", PMA_TRANSCRIPT_VERSION)
        payload.setdefault("turn_id", turn_id)
        payload.setdefault("created_at", now_iso())
        payload["metadata_path"] = str(json_path)
        payload["content_path"] = str(md_path)
        payload["assistant_text_chars"] = len(assistant_text or "")
        resolved_user_text = user_text
        if resolved_user_text is None:
            raw_user_prompt = payload.get("user_prompt")
            if isinstance(raw_user_prompt, str):
                resolved_user_text = raw_user_prompt
        if resolved_user_text:
            payload["user_text_chars"] = len(resolved_user_text)

        redacted_user_text = (
            redact_text(resolved_user_text) if resolved_user_text is not None else None
        )
        redacted_assistant_text = redact_text(assistant_text or "")
        redacted_payload, metadata_redacted = redact_jsonable(payload)
        payload = dict(redacted_payload)
        existing_redactions = payload.get("redactions_applied") or []
        if not isinstance(existing_redactions, list):
            existing_redactions = []
        redactions_applied = {str(item) for item in existing_redactions}
        if (
            metadata_redacted
            or redacted_user_text != resolved_user_text
            or redacted_assistant_text != (assistant_text or "")
        ):
            redactions_applied.add("secret-patterns")
        if redactions_applied:
            payload["redactions_applied"] = sorted(redactions_applied)

        self._dir.mkdir(parents=True, exist_ok=True)
        transcript_content = build_plain_text_transcript(
            user_text=redacted_user_text or "",
            assistant_text=redacted_assistant_text,
        )
        atomic_write(md_path, transcript_content + "\n")
        atomic_write(json_path, json.dumps(payload, indent=2) + "\n")
        self._mirror_store.write_mirror(
            turn_id=turn_id,
            metadata=payload,
            user_text=redacted_user_text,
            assistant_text=redacted_assistant_text,
        )

        return PmaTranscriptPointer(
            turn_id=turn_id,
            metadata_path=str(json_path),
            content_path=str(md_path),
            created_at=payload["created_at"],
        )

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._mirror_store.list_recent(limit=limit)

    def read_transcript(self, turn_id: str) -> Optional[dict[str, Any]]:
        return self._mirror_store.read_transcript(turn_id)


class PmaTranscriptLegacyBackfill:
    """One-time importer for pre-mirror PMA transcript files.

    Normal `PmaTranscriptStore` reads intentionally do not inspect legacy
    Markdown/JSON files. Operators with old transcript files can run this
    service once before relying on mirror-only reads.
    """

    def __init__(self, hub_root: Path) -> None:
        self._dir = default_pma_transcripts_dir(hub_root)
        self._mirror_store = TranscriptMirrorStore(hub_root)

    def run(self) -> PmaTranscriptBackfillResult:
        if not self._dir.exists():
            return PmaTranscriptBackfillResult(imported_count=0, skipped_count=0)

        imported_count = 0
        skipped_count = 0
        for metadata_path in sorted(self._dir.glob("*.json")):
            meta = self._read_metadata(metadata_path)
            if meta is None:
                skipped_count += 1
                continue
            turn_id = str(meta.get("turn_id") or "").strip()
            if not turn_id:
                skipped_count += 1
                continue
            content = self._read_content(meta, metadata_path)
            self._mirror_store.write_mirror_content(
                turn_id=turn_id,
                metadata=meta,
                text_content=content,
            )
            imported_count += 1
        return PmaTranscriptBackfillResult(
            imported_count=imported_count,
            skipped_count=skipped_count,
        )

    def _read_content(self, meta: dict[str, Any], metadata_path: Path) -> str:
        content_path_text = str(meta.get("content_path") or "").strip()
        if not content_path_text:
            return ""
        content_path = Path(content_path_text)
        if not content_path.is_absolute():
            content_path = (metadata_path.parent / content_path).resolve()
        try:
            return content_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to read PMA transcript content at %s: %s", content_path, exc
            )
            return ""

    def _read_metadata(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to read PMA transcript metadata at %s: %s", path, exc
            )
            return None
        return data if isinstance(data, dict) else None


__all__ = [
    "PMA_TRANSCRIPTS_DIRNAME",
    "PMA_TRANSCRIPT_PREVIEW_CHARS",
    "PMA_TRANSCRIPT_VERSION",
    "PmaTranscriptBackfillResult",
    "PmaTranscriptLegacyBackfill",
    "PmaTranscriptPointer",
    "PmaTranscriptStore",
    "default_pma_transcripts_dir",
]
