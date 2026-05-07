from __future__ import annotations

from typing import Any

from ..document_file_intents import normalize_document_file_intents


def normalize_managed_thread_attachments(value: Any) -> list[dict[str, Any]]:
    return normalize_document_file_intents(value)


__all__ = ["normalize_managed_thread_attachments"]
