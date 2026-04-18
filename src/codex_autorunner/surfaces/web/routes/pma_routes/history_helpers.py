"""PMA history/files/docs/dispatch response assembly helpers.

Extracts repeated dispatch-item serialization and doc-name normalization
previously inlined in history_files_docs.py.
"""

from __future__ import annotations

from typing import Any

from .....core.pma_dispatches import PmaDispatch


def serialize_dispatch_item(item: PmaDispatch) -> dict[str, Any]:
    return {
        "id": item.dispatch_id,
        "title": item.title,
        "body": item.body,
        "priority": item.priority,
        "links": item.links,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
        "source_turn_id": item.source_turn_id,
    }


def sorted_doc_names(docs_dir_entries: set[str]) -> list[str]:
    ordered: list[str] = []
    for doc_name in (
        "AGENTS.md",
        "active_context.md",
        "context_log.md",
        "ABOUT_CAR.md",
        "prompt.md",
    ):
        if doc_name in docs_dir_entries:
            ordered.append(doc_name)
    remaining = sorted(name for name in docs_dir_entries if name not in ordered)
    ordered.extend(remaining)
    return ordered
