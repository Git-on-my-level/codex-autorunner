"""Platform-agnostic media/voice abstractions and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

IMAGE_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
IMAGE_EXTS = set(IMAGE_CONTENT_TYPES.values())


@dataclass(frozen=True)
class ChatVoiceInput:
    """Normalized voice/audio input metadata."""

    kind: str
    file_id: str
    mime_type: Optional[str] = None
    file_name: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_seconds: Optional[int] = None
    transcript: Optional[str] = None


def is_image_mime_or_path(mime_type: Optional[str], file_name: Optional[str]) -> bool:
    """Return whether a media item should be treated as image-like."""

    if mime_type:
        base = mime_type.lower().split(";", 1)[0].strip()
        if base.startswith("image/"):
            return True
    if file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix in IMAGE_EXTS:
            return True
    return False


def format_media_batch_failure(
    *,
    image_disabled: int,
    file_disabled: int,
    image_too_large: int,
    file_too_large: int,
    image_download_failed: int,
    file_download_failed: int,
    image_download_detail: Optional[str] = None,
    file_download_detail: Optional[str] = None,
    image_save_failed: int,
    file_save_failed: int,
    unsupported: int,
    max_image_bytes: int,
    max_file_bytes: int,
) -> str:
    """Format a batch-media failure summary message."""

    base = "Failed to process any media in the batch."
    details: list[str] = []
    if image_disabled:
        details.append(f"{image_disabled} image(s) skipped (image handling disabled).")
    if file_disabled:
        details.append(f"{file_disabled} file(s) skipped (file handling disabled).")
    if image_too_large:
        details.append(
            f"{image_too_large} image(s) too large (max {max_image_bytes} bytes)."
        )
    if file_too_large:
        details.append(
            f"{file_too_large} file(s) too large (max {max_file_bytes} bytes)."
        )
    if image_download_failed:
        line = f"{image_download_failed} image(s) failed to download."
        if image_download_detail:
            label = "error" if image_download_failed == 1 else "last error"
            line = f"{line} ({label}: {image_download_detail})"
        details.append(line)
    if file_download_failed:
        line = f"{file_download_failed} file(s) failed to download."
        if file_download_detail:
            label = "error" if file_download_failed == 1 else "last error"
            line = f"{line} ({label}: {file_download_detail})"
        details.append(line)
    if image_save_failed:
        details.append(f"{image_save_failed} image(s) failed to save.")
    if file_save_failed:
        details.append(f"{file_save_failed} file(s) failed to save.")
    if unsupported:
        details.append(f"{unsupported} item(s) had unsupported media types.")
    if not details:
        return base
    return f"{base}\n" + "\n".join(f"- {line}" for line in details)
