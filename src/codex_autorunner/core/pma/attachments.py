from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

from ..document_file_intents import normalize_document_file_intents
from ..filebox import inbox_dir, sanitize_filename


@dataclass(frozen=True)
class ManagedThreadAttachmentExecutionContext:
    prompt_text: str
    input_items: list[dict[str, Any]] | None = None


def normalize_managed_thread_attachments(value: Any) -> list[dict[str, Any]]:
    return normalize_document_file_intents(value)


def build_managed_thread_attachment_execution_context(
    attachments: list[dict[str, Any]],
    *,
    hub_root: Path,
) -> ManagedThreadAttachmentExecutionContext | None:
    """Render web PMA attachment metadata into the actual runtime turn input.

    Managed thread uploads are persisted to the hub FileBox before the message is sent.
    The visible chat chip is not enough: the runtime prompt must include the local
    file path so the agent can inspect it, and images should also travel as native
    image inputs when the backend supports them.
    """

    if not attachments:
        return None
    lines: list[str] = ["PMA File Inbox:"]
    input_items: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, Mapping):
            continue
        title = _attachment_text(
            attachment.get("title")
            or attachment.get("uploadedName")
            or attachment.get("uploaded_name")
            or attachment.get("url")
            or attachment.get("path")
            or f"attachment-{index}"
        )
        intent = _attachment_text(attachment.get("intent"))
        source = _attachment_text(attachment.get("source"))
        url = _attachment_text(attachment.get("url"))
        rel_path = _attachment_text(attachment.get("path"))
        mime_type = _attachment_text(
            attachment.get("mime_type") or attachment.get("mimeType")
        )
        size_label = _attachment_text(
            attachment.get("size_label") or attachment.get("sizeLabel")
        )
        size_bytes = attachment.get("size_bytes") or attachment.get("sizeBytes")
        local_path = _resolve_uploaded_attachment_path(attachment, hub_root=hub_root)

        lines.append(f"- {title}")
        if intent:
            lines.append(f"  Intent: {intent}")
        if source:
            lines.append(f"  Source: {source}")
        if rel_path:
            lines.append(f"  Path: {rel_path}")
        if url:
            lines.append(f"  URL: {url}")
        if local_path is not None:
            lines.append(f"  Saved to: {local_path}")
        if mime_type:
            lines.append(f"  Mime: {mime_type}")
        if isinstance(size_bytes, int) and not isinstance(size_bytes, bool):
            lines.append(f"  Size: {size_bytes} bytes")
        elif size_label:
            lines.append(f"  Size: {size_label}")

        if (
            local_path is not None
            and local_path.is_file()
            and _is_image_attachment(
                title=title,
                mime_type=mime_type,
                path=local_path,
            )
        ):
            input_items.append({"type": "localImage", "path": str(local_path)})

    if len(lines) == 1:
        return None
    return ManagedThreadAttachmentExecutionContext(
        prompt_text="\n".join(lines),
        input_items=input_items or None,
    )


def _resolve_uploaded_attachment_path(
    attachment: Mapping[str, Any],
    *,
    hub_root: Path,
) -> Path | None:
    source = (_attachment_text(attachment.get("source")) or "").lower()
    intent = (_attachment_text(attachment.get("intent")) or "").lower()
    if source != "upload" and intent != "attach_uploaded_file":
        return None
    filename = _attachment_text(
        attachment.get("uploaded_name")
        or attachment.get("uploadedName")
        or attachment.get("name")
    )
    if filename is None:
        filename = _filename_from_pma_file_url(_attachment_text(attachment.get("url")))
    if filename is None:
        return None
    try:
        safe_name = sanitize_filename(filename)
    except ValueError:
        return None
    return inbox_dir(hub_root) / safe_name


def _filename_from_pma_file_url(url: str | None) -> str | None:
    if url is None:
        return None
    marker = "/hub/pma/files/inbox/"
    if marker not in url:
        return None
    return unquote(url.rsplit("/", 1)[-1])


def _is_image_attachment(
    *,
    title: str | None,
    mime_type: str | None,
    path: Path,
) -> bool:
    if mime_type and mime_type.lower().startswith("image/"):
        return True
    guessed = mimetypes.guess_type(str(path))[0] or mimetypes.guess_type(title or "")[0]
    return bool(guessed and guessed.lower().startswith("image/"))


def _attachment_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "ManagedThreadAttachmentExecutionContext",
    "build_managed_thread_attachment_execution_context",
    "normalize_managed_thread_attachments",
]
