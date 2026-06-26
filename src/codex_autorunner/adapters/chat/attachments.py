"""Shared inbound attachment transcript metadata helpers."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Optional


def build_inbound_attachment_metadata(
    *,
    path: Path,
    original_name: str,
    source_surface: str,
    source_message_id: Optional[str] = None,
    source_thread_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    box: str = "inbox",
    kind: Optional[str] = None,
) -> dict[str, Any]:
    """Return compact metadata suitable for managed-turn transcript payloads."""

    saved_path = Path(path)
    name = saved_path.name
    title = str(original_name or name).strip() or name
    resolved_mime = _normalize_optional_text(mime_type) or (
        mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    resolved_kind = _normalize_optional_text(kind) or _kind_from_mime(resolved_mime)
    size = size_bytes
    if size is None:
        try:
            size = saved_path.stat().st_size
        except OSError:
            size = None
    payload: dict[str, Any] = {
        "id": f"{source_surface}:{source_message_id or name}:{name}",
        "kind": resolved_kind,
        "title": title,
        "name": name,
        "filename": name,
        "box": box,
        "source": "surface_upload",
        "source_surface": source_surface,
        "mime_type": resolved_mime,
    }
    if size is not None:
        payload["size"] = size
        payload["size_bytes"] = size
    if source_message_id:
        payload["source_message_id"] = source_message_id
    if source_thread_id:
        payload["source_thread_id"] = source_thread_id
    return payload


def _kind_from_mime(mime_type: str) -> str:
    lowered = mime_type.lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    return "file"


def _normalize_optional_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


__all__ = ["build_inbound_attachment_metadata"]
