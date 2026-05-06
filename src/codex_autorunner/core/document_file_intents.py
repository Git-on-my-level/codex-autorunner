from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, cast

from .safe_paths import SafePathError, validate_relative_posix_path

DocumentFileIntentKind = Literal[
    "browse_source",
    "select_item",
    "attach_uploaded_file",
    "reference_path",
    "include_link",
    "remove_pending_attachment",
    "clear_pending_attachments",
]

DocumentFileSource = Literal[
    "tickets",
    "contextspace",
    "filebox",
    "workspace",
    "upload",
    "link",
]

_INTENT_KINDS: set[str] = {
    "browse_source",
    "select_item",
    "attach_uploaded_file",
    "reference_path",
    "include_link",
    "remove_pending_attachment",
    "clear_pending_attachments",
}

_SOURCES: set[str] = {
    "tickets",
    "contextspace",
    "filebox",
    "workspace",
    "upload",
    "link",
}


@dataclass(frozen=True)
class DocumentFileIntent:
    """Surface-neutral document/file selection or attachment intent."""

    intent: DocumentFileIntentKind
    source: Optional[DocumentFileSource] = None
    id: Optional[str] = None
    title: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    size_label: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"intent": self.intent}
        for key in (
            "source",
            "id",
            "title",
            "path",
            "url",
            "mime_type",
            "size_bytes",
            "size_label",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def normalize_document_file_intents(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    intents: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        intent = normalize_document_file_intent(
            item, fallback_id=f"attachment-{index + 1}"
        )
        if intent is not None:
            intents.append(intent)
    return intents


def normalize_document_file_intent(
    item: Mapping[str, Any],
    *,
    fallback_id: str = "attachment-1",
) -> dict[str, Any] | None:
    raw_intent = _text(item.get("intent") or item.get("action"))
    kind = _normalize_intent_kind(raw_intent, item)
    if kind is None:
        return None

    title = _text(item.get("title") or item.get("name") or item.get("uploadedName"))
    url = _text(item.get("url") or item.get("href"))
    uploaded_name = _text(
        item.get("uploadedName") or item.get("uploaded_name") or item.get("name")
    )
    path = _normalize_path(_text(item.get("path") or item.get("rel_path")))
    source = _normalize_source(item.get("source"), kind=kind, path=path, url=url)
    intent_id = _text(item.get("id") or item.get("attachment_id")) or fallback_id
    size_label = _text(item.get("sizeLabel") or item.get("size_label"))
    mime_type = _text(item.get("mimeType") or item.get("mime_type"))
    size_bytes = _normalize_size_bytes(item.get("sizeBytes") or item.get("size_bytes"))

    if kind == "clear_pending_attachments":
        return DocumentFileIntent(intent=kind, source=source).to_payload()

    if kind == "remove_pending_attachment":
        if not intent_id:
            return None
        return DocumentFileIntent(intent=kind, source=source, id=intent_id).to_payload()

    if kind == "include_link" and url is None:
        return None

    if kind == "attach_uploaded_file" and uploaded_name is None and path is None:
        return None

    if kind in {"reference_path", "select_item"} and path is None and intent_id is None:
        return None

    metadata = _metadata(item)
    if uploaded_name is not None:
        metadata["uploaded_name"] = uploaded_name
        metadata["uploadedName"] = uploaded_name
    legacy_kind = _legacy_attachment_kind(item, kind)
    if legacy_kind is not None:
        metadata["kind"] = legacy_kind
    upload_state = _text(item.get("uploadState") or item.get("upload_state"))
    if upload_state is not None:
        metadata["upload_state"] = upload_state
        metadata["uploadState"] = upload_state

    intent = DocumentFileIntent(
        intent=kind,
        source=source,
        id=intent_id,
        title=title or uploaded_name or url or path or fallback_id,
        path=path,
        url=url,
        mime_type=mime_type,
        size_bytes=size_bytes,
        size_label=size_label,
        metadata=metadata,
    ).to_payload()
    return _with_legacy_attachment_fields(intent)


def document_reference_intent(
    *,
    source: DocumentFileSource,
    document_id: str,
    title: str,
    rel_path: str,
) -> DocumentFileIntent:
    return DocumentFileIntent(
        intent="reference_path",
        source=source,
        id=_text(document_id),
        title=_text(title),
        path=_normalize_path(_text(rel_path)),
    )


def _normalize_intent_kind(
    raw_intent: str | None,
    item: Mapping[str, Any],
) -> DocumentFileIntentKind | None:
    if raw_intent is not None:
        normalized = raw_intent.strip().lower()
        if normalized in _INTENT_KINDS:
            return cast(DocumentFileIntentKind, normalized)
        return None
    legacy_kind = (_text(item.get("kind")) or "file").lower()
    if legacy_kind == "link" or _text(item.get("href")) is not None:
        return "include_link"
    if legacy_kind in {"file", "image"}:
        return "attach_uploaded_file"
    if _text(item.get("path") or item.get("rel_path")) is not None:
        return "reference_path"
    return None


def _normalize_source(
    value: Any,
    *,
    kind: DocumentFileIntentKind,
    path: str | None,
    url: str | None,
) -> DocumentFileSource | None:
    raw = _text(value)
    if raw is not None:
        normalized = raw.lower()
        if normalized in _SOURCES:
            return cast(DocumentFileSource, normalized)
    if kind == "attach_uploaded_file":
        return "upload"
    if kind == "include_link" or url is not None:
        return "link"
    if path:
        if path.startswith(".codex-autorunner/tickets/"):
            return "tickets"
        if path.startswith(".codex-autorunner/contextspace/"):
            return "contextspace"
        if path.startswith(".codex-autorunner/filebox/"):
            return "filebox"
        return "workspace"
    return None


def _normalize_path(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_relative_posix_path(value).as_posix()
    except SafePathError as exc:
        raise ValueError(str(exc)) from exc


def _legacy_attachment_kind(
    item: Mapping[str, Any],
    kind: DocumentFileIntentKind,
) -> str | None:
    raw = (_text(item.get("kind")) or "").lower()
    if raw in {"file", "image", "link"}:
        return raw
    if kind == "include_link":
        return "link"
    if kind == "attach_uploaded_file":
        return "file"
    return None


def _with_legacy_attachment_fields(intent: dict[str, Any]) -> dict[str, Any]:
    payload = dict(intent)
    metadata_raw = payload.get("metadata")
    metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
    kind = metadata.get("kind")
    if isinstance(kind, str):
        payload["kind"] = kind
    if "size_label" in payload:
        payload["sizeLabel"] = payload["size_label"]
    uploaded_name = metadata.get("uploaded_name")
    if isinstance(uploaded_name, str):
        payload["uploaded_name"] = uploaded_name
        payload["uploadedName"] = uploaded_name
    upload_state = metadata.get("upload_state")
    if isinstance(upload_state, str):
        payload["upload_state"] = upload_state
        payload["uploadState"] = upload_state
    return payload


def _metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    raw = item.get("metadata")
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_size_bytes(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "DocumentFileIntent",
    "DocumentFileIntentKind",
    "DocumentFileSource",
    "document_reference_intent",
    "normalize_document_file_intent",
    "normalize_document_file_intents",
]
