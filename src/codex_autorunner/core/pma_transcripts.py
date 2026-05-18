from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .orchestration.transcript_mirror import TranscriptMirrorStore
from .redaction import redact_jsonable, redact_text
from .time_utils import now_iso

logger = logging.getLogger(__name__)

PMA_TRANSCRIPTS_DIRNAME = "transcripts"
PMA_TRANSCRIPT_VERSION = 1
PMA_TRANSCRIPT_PREVIEW_CHARS = 400


def default_pma_transcripts_dir(hub_root: Path) -> Path:
    return hub_root / ".codex-autorunner" / "pma" / PMA_TRANSCRIPTS_DIRNAME


@dataclass(frozen=True)
class PmaTranscriptPointer:
    turn_id: str
    transcript_mirror_id: str
    created_at: str


@dataclass(frozen=True)
class PmaTranscriptBackfillResult:
    imported_count: int
    skipped_count: int
    error_count: int


@dataclass(frozen=True)
class PmaTranscriptCoverageStatus:
    operation: str
    scope: str
    owner: str
    canonical_store: str
    legacy_primary_path: bool
    mirrored_count: int
    legacy_metadata_files_count: int
    legacy_unmirrored_files_count: int
    last_backfill_status: dict[str, Any] | None


class PmaTranscriptStore:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
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
        payload = dict(metadata)
        payload.setdefault("version", PMA_TRANSCRIPT_VERSION)
        payload.setdefault("turn_id", turn_id)
        payload.setdefault("created_at", now_iso())
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

        self._mirror_store.write_mirror(
            turn_id=turn_id,
            metadata=payload,
            user_text=redacted_user_text,
            assistant_text=redacted_assistant_text,
        )

        return PmaTranscriptPointer(
            turn_id=turn_id,
            transcript_mirror_id=turn_id,
            created_at=payload["created_at"],
        )

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._mirror_store.list_recent(limit=limit)

    def read_transcript(self, turn_id: str) -> Optional[dict[str, Any]]:
        return self._mirror_store.read_transcript(turn_id)

    def coverage_status(self) -> PmaTranscriptCoverageStatus:
        return PmaTranscriptLegacyBackfill(self._hub_root).coverage_status()


class PmaTranscriptLegacyBackfill:
    """One-time importer for pre-mirror PMA transcript files.

    Normal `PmaTranscriptStore` reads intentionally do not inspect legacy
    Markdown/JSON files. Operators with old transcript files can run this
    service once before relying on mirror-only reads.
    """

    _STATUS_KEY = "pma_transcript_legacy_backfill_v1"

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._dir = default_pma_transcripts_dir(hub_root)
        self._mirror_store = TranscriptMirrorStore(hub_root)

    def run(self, *, conn: Any | None = None) -> PmaTranscriptBackfillResult:
        if not self._dir.exists():
            result = PmaTranscriptBackfillResult(
                imported_count=0, skipped_count=0, error_count=0
            )
            self._record_status(result, conn=conn)
            return result

        imported_count = 0
        skipped_count = 0
        error_count = 0
        for metadata_path in sorted(self._dir.glob("*.json")):
            meta = self._read_metadata(metadata_path)
            if meta is None:
                error_count += 1
                continue
            turn_id = str(meta.get("turn_id") or "").strip()
            if not turn_id:
                logger.warning(
                    "Skipping PMA transcript metadata without turn_id at %s",
                    metadata_path,
                )
                error_count += 1
                continue
            if turn_id in self._mirrored_ids(conn=conn):
                skipped_count += 1
                continue
            content = self._read_content(meta, metadata_path)
            self._mirror_store.write_mirror_content(
                turn_id=turn_id,
                metadata=meta,
                text_content=content,
                conn=conn,
            )
            imported_count += 1
        result = PmaTranscriptBackfillResult(
            imported_count=imported_count,
            skipped_count=skipped_count,
            error_count=error_count,
        )
        self._record_status(result, conn=conn)
        return result

    def coverage_status(self) -> PmaTranscriptCoverageStatus:
        legacy_metadata_paths = list(self._legacy_metadata_paths())
        mirrored_ids = self._mirrored_ids(conn=None)
        legacy_unmirrored = 0
        for metadata_path in legacy_metadata_paths:
            meta = self._read_metadata(metadata_path, log_errors=False)
            if meta is None:
                legacy_unmirrored += 1
                continue
            turn_id = str(meta.get("turn_id") or "").strip()
            if not turn_id or turn_id not in mirrored_ids:
                legacy_unmirrored += 1
        return PmaTranscriptCoverageStatus(
            operation="pma_transcript_mirror_coverage",
            scope="pma",
            owner="orchestration_transcript_mirrors",
            canonical_store="orch_transcript_mirrors",
            legacy_primary_path=False,
            mirrored_count=len(mirrored_ids),
            legacy_metadata_files_count=len(legacy_metadata_paths),
            legacy_unmirrored_files_count=legacy_unmirrored,
            last_backfill_status=self._read_last_status(),
        )

    def _legacy_metadata_paths(self) -> list[Path]:
        if not self._dir.exists():
            return []
        return sorted(self._dir.glob("*.json"))

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

    def _read_metadata(
        self, path: Path, *, log_errors: bool = True
    ) -> Optional[dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            if log_errors:
                logger.warning(
                    "Failed to read PMA transcript metadata at %s: %s", path, exc
                )
            return None
        if not isinstance(data, dict):
            if log_errors:
                logger.warning(
                    "Skipping PMA transcript metadata with non-object payload at %s",
                    path,
                )
            return None
        return data

    def _mirrored_ids(self, *, conn: Any | None) -> set[str]:
        try:
            if conn is not None:
                rows = conn.execute(
                    "SELECT transcript_mirror_id FROM orch_transcript_mirrors"
                ).fetchall()
            else:
                with open_orchestration_sqlite(
                    self._hub_root, migrate=False
                ) as owned_conn:
                    rows = owned_conn.execute(
                        "SELECT transcript_mirror_id FROM orch_transcript_mirrors"
                    ).fetchall()
        except Exception:
            return set()
        return {str(row["transcript_mirror_id"]) for row in rows}

    def _record_status(
        self,
        result: PmaTranscriptBackfillResult,
        *,
        conn: Any | None,
    ) -> None:
        completed_at = now_iso()
        payload = {
            "imported_count": result.imported_count,
            "skipped_count": result.skipped_count,
            "error_count": result.error_count,
            "completed_at": completed_at,
        }
        status_path = self._dir.parent / "transcript_backfill_status.json"
        try:
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "Failed to write PMA transcript backfill status at %s", status_path
            )
        try:
            target_conn = conn
            if target_conn is None:
                with open_orchestration_sqlite(self._hub_root) as owned_conn:
                    self._record_status_flag(owned_conn, completed_at)
                return
            self._record_status_flag(target_conn, completed_at)
        except Exception:
            logger.warning("Failed to record PMA transcript backfill status flag")

    def _record_status_flag(self, conn: Any, completed_at: str) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO orch_operation_flags (flag_key, completed_at)
            VALUES (?, ?)
            """,
            (self._STATUS_KEY, completed_at),
        )

    def _read_last_status(self) -> dict[str, Any] | None:
        status_path = self._dir.parent / "transcript_backfill_status.json"
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            return payload
        try:
            with open_orchestration_sqlite(self._hub_root, migrate=False) as conn:
                row = conn.execute(
                    """
                    SELECT completed_at
                      FROM orch_operation_flags
                     WHERE flag_key = ?
                    """,
                    (self._STATUS_KEY,),
                ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return {"completed_at": row["completed_at"]}


__all__ = [
    "PMA_TRANSCRIPTS_DIRNAME",
    "PMA_TRANSCRIPT_PREVIEW_CHARS",
    "PMA_TRANSCRIPT_VERSION",
    "PmaTranscriptBackfillResult",
    "PmaTranscriptCoverageStatus",
    "PmaTranscriptLegacyBackfill",
    "PmaTranscriptPointer",
    "PmaTranscriptStore",
    "default_pma_transcripts_dir",
]
