from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, cast

from ..core import drafts as draft_utils
from ..core.logging_utils import log_event
from ..core.state_roots import resolve_repo_state_root
from .catalog import CONTEXTSPACE_DOC_CATALOG, ContextspaceDocCatalogEntry

ContextspaceDocKind = Literal["active_context", "decisions", "spec"]
CONTEXTSPACE_DOC_KINDS: tuple[ContextspaceDocKind, ...] = tuple(
    cast(ContextspaceDocKind, entry.kind) for entry in CONTEXTSPACE_DOC_CATALOG
)

logger = logging.getLogger(__name__)


def normalize_contextspace_doc_kind(kind: str) -> ContextspaceDocKind:
    key = (kind or "").strip().lower()
    if key not in CONTEXTSPACE_DOC_KINDS:
        raise ValueError("invalid contextspace doc kind")
    return cast(ContextspaceDocKind, key)


def contextspace_doc_catalog() -> tuple[ContextspaceDocCatalogEntry, ...]:
    return CONTEXTSPACE_DOC_CATALOG


def contextspace_doc_entry(kind: str) -> ContextspaceDocCatalogEntry:
    key = normalize_contextspace_doc_kind(kind)
    for entry in CONTEXTSPACE_DOC_CATALOG:
        if entry.kind == key:
            return entry
    raise ValueError("invalid contextspace doc kind")


def contextspace_dir(repo_root: Path) -> Path:
    return resolve_repo_state_root(repo_root) / "contextspace"


def contextspace_doc_path(repo_root: Path, kind: str) -> Path:
    return contextspace_dir(repo_root) / contextspace_doc_entry(kind).path


def read_contextspace_doc(repo_root: Path, kind: str) -> str:
    path = contextspace_doc_path(repo_root, kind)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_contextspace_docs(repo_root: Path) -> dict[ContextspaceDocKind, str]:
    return {
        cast(ContextspaceDocKind, entry.kind): read_contextspace_doc(
            repo_root, entry.kind
        )
        for entry in CONTEXTSPACE_DOC_CATALOG
    }


def serialize_contextspace_doc_catalog() -> list[dict[str, str]]:
    return [entry.as_dict() for entry in CONTEXTSPACE_DOC_CATALOG]


def write_contextspace_doc(repo_root: Path, kind: str, content: str) -> str:
    path = contextspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    rel = path.relative_to(repo_root).as_posix()
    state_key = f"contextspace_{path.name}"
    try:
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        draft_utils.remove_draft(repo_root, state_key)
    except (
        Exception
    ) as exc:  # intentional: best-effort draft cleanup across unpredictable utility internals
        log_event(
            logger,
            logging.WARNING,
            "contextspace.draft_invalidation_failed",
            repo_root=str(repo_root),
            rel_path=rel,
            state_key=state_key,
            kind=kind,
            exc=exc,
        )
        logger.debug(
            "contextspace draft invalidation failed for %s (repo_root=%s kind=%s)",
            rel,
            repo_root,
            kind,
            exc_info=True,
        )
    return path.read_text(encoding="utf-8")


__all__ = [
    "CONTEXTSPACE_DOC_KINDS",
    "ContextspaceDocKind",
    "contextspace_doc_catalog",
    "contextspace_doc_entry",
    "contextspace_dir",
    "contextspace_doc_path",
    "normalize_contextspace_doc_kind",
    "read_contextspace_doc",
    "read_contextspace_docs",
    "serialize_contextspace_doc_catalog",
    "write_contextspace_doc",
]
