"""Platform-agnostic media/voice abstractions and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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

AUDIO_CONTENT_TYPES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}
AUDIO_EXT_TO_CONTENT_TYPE = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}
GENERIC_BINARY_MIME_TYPES = {"application/octet-stream", "binary/octet-stream"}


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


def normalize_mime_type(mime_type: Optional[str]) -> Optional[str]:
    if not mime_type:
        return None
    base = mime_type.lower().split(";", 1)[0].strip()
    return base or None


def audio_content_type_for_input(
    *,
    mime_type: Optional[str],
    file_name: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Optional[str]:
    """
    Return an audio-safe content type for transcription when one can be inferred.

    Generic binary MIME values are treated as unknown and return None unless a known
    audio extension can be inferred from file_name or source_url.
    """

    mime_base = normalize_mime_type(mime_type)
    if mime_base == "video/webm":
        return "audio/webm"
    if mime_base == "video/mp4":
        return "audio/mp4"
    if mime_base in ("audio/mp4a-latm", "audio/x-m4a"):
        return "audio/mp4"
    if mime_base == "audio/x-wav":
        return "audio/wav"
    if mime_base and mime_base.startswith("audio/"):
        return mime_base

    for candidate in (file_name, _basename_from_url(source_url)):
        suffix = _suffix(candidate)
        if suffix in AUDIO_EXT_TO_CONTENT_TYPE:
            return AUDIO_EXT_TO_CONTENT_TYPE[suffix]

    if mime_base in GENERIC_BINARY_MIME_TYPES:
        return None
    return None


def is_audio_mime_or_path(
    *,
    mime_type: Optional[str],
    file_name: Optional[str] = None,
    source_url: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    kind: Optional[str] = None,
) -> bool:
    kind_base = (kind or "").strip().lower()
    if kind_base in {"audio", "voice"}:
        return True
    if kind_base in {"image", "video"}:
        return False

    mime_base = normalize_mime_type(mime_type)
    if mime_base and mime_base.startswith("audio/"):
        return True
    if mime_base and (mime_base.startswith("image/") or mime_base.startswith("video/")):
        return False

    if audio_content_type_for_input(
        mime_type=mime_type,
        file_name=file_name,
        source_url=source_url,
    ):
        return True
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        return True
    return False


def audio_extension_for_input(
    *,
    mime_type: Optional[str],
    file_name: Optional[str] = None,
    source_url: Optional[str] = None,
    default: str = "",
) -> str:
    for candidate in (file_name, _basename_from_url(source_url)):
        suffix = _suffix(candidate)
        if suffix in AUDIO_EXT_TO_CONTENT_TYPE:
            return suffix

    content_type = audio_content_type_for_input(
        mime_type=mime_type,
        file_name=file_name,
        source_url=source_url,
    )
    if content_type:
        mapped = AUDIO_CONTENT_TYPES.get(content_type)
        if mapped:
            return mapped
    return default


def _basename_from_url(url: Optional[str]) -> Optional[str]:
    if not isinstance(url, str) or not url:
        return None
    return Path(urlparse(url).path).name


def _suffix(candidate: Optional[str]) -> str:
    if not isinstance(candidate, str) or not candidate:
        return ""
    return Path(candidate).suffix.lower()


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
