from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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


def _read_preview(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read(PMA_TRANSCRIPT_PREVIEW_CHARS + 1)
    except OSError as exc:
        logger.warning("Failed to read PMA transcript content at %s: %s", path, exc)
        return ""
    text = text.strip()
    if len(text) <= PMA_TRANSCRIPT_PREVIEW_CHARS:
        return text
    return text[:PMA_TRANSCRIPT_PREVIEW_CHARS].rstrip() + "..."


@dataclass(frozen=True)
class PmaTranscriptPointer:
    turn_id: str
    metadata_path: str
    content_path: str
    created_at: str


class PmaTranscriptStore:
    def __init__(self, hub_root: Path) -> None:
        self._root = hub_root
        self._dir = default_pma_transcripts_dir(hub_root)

    @property
    def dir(self) -> Path:
        return self._dir

    def write_transcript(
        self,
        *,
        turn_id: str,
        metadata: dict[str, Any],
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

        self._dir.mkdir(parents=True, exist_ok=True)
        atomic_write(md_path, (assistant_text or "") + "\n")
        atomic_write(json_path, json.dumps(payload, indent=2) + "\n")

        return PmaTranscriptPointer(
            turn_id=turn_id,
            metadata_path=str(json_path),
            content_path=str(md_path),
            created_at=payload["created_at"],
        )

    def list_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        if not self._dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.json"), reverse=True):
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Failed to read PMA transcript metadata at %s: %s", path, exc
                )
                continue
            if not isinstance(data, dict):
                continue
            content_path = Path(str(data.get("content_path") or ""))
            if not content_path.is_absolute():
                content_path = (path.parent / content_path).resolve()
            data = dict(data)
            data["preview"] = _read_preview(content_path)
            entries.append(data)
            if len(entries) >= limit:
                break
        return entries

    def read_transcript(self, turn_id: str) -> Optional[dict[str, Any]]:
        match = self._find_metadata(turn_id)
        if not match:
            return None
        meta, meta_path = match
        content_path = Path(str(meta.get("content_path") or ""))
        if not content_path.is_absolute():
            content_path = (meta_path.parent / content_path).resolve()
        try:
            content = content_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to read PMA transcript content at %s: %s", content_path, exc
            )
            content = ""
        return {"metadata": meta, "content": content}

    def _find_metadata(self, turn_id: str) -> Optional[tuple[dict[str, Any], Path]]:
        if not self._dir.exists():
            return None
        safe_turn_id = _safe_segment(turn_id)
        candidates = sorted(self._dir.glob(f"*_{safe_turn_id}.json"), reverse=True)
        for path in candidates:
            meta = self._read_metadata(path)
            if meta and str(meta.get("turn_id")) == turn_id:
                return meta, path
        if candidates:
            meta = self._read_metadata(candidates[0])
            if meta:
                return meta, candidates[0]
        for path in sorted(self._dir.glob("*.json"), reverse=True):
            meta = self._read_metadata(path)
            if meta and str(meta.get("turn_id")) == turn_id:
                return meta, path
        return None

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
    "PmaTranscriptPointer",
    "PmaTranscriptStore",
    "default_pma_transcripts_dir",
]
