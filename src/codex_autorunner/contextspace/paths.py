from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, cast

from ..core import drafts as draft_utils
from ..core.logging_utils import log_event
from ..core.state_roots import resolve_repo_state_root

ContextspaceDocKind = Literal["active_context", "decisions", "spec"]
CONTEXTSPACE_DOC_KINDS: tuple[ContextspaceDocKind, ...] = (
    "active_context",
    "decisions",
    "spec",
)

logger = logging.getLogger(__name__)


def normalize_contextspace_doc_kind(kind: str) -> ContextspaceDocKind:
    key = (kind or "").strip().lower()
    if key not in CONTEXTSPACE_DOC_KINDS:
        raise ValueError("invalid contextspace doc kind")
    return cast(ContextspaceDocKind, key)


def contextspace_dir(repo_root: Path) -> Path:
    return resolve_repo_state_root(repo_root) / "contextspace"


def contextspace_doc_path(repo_root: Path, kind: str) -> Path:
    key = normalize_contextspace_doc_kind(kind)
    return contextspace_dir(repo_root) / f"{key}.md"


def read_contextspace_doc(repo_root: Path, kind: str) -> str:
    path = contextspace_doc_path(repo_root, kind)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_contextspace_doc(repo_root: Path, kind: str, content: str) -> str:
    path = contextspace_doc_path(repo_root, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")
    rel = path.relative_to(repo_root).as_posix()
    state_key = f"contextspace_{path.name}"
    try:
        draft_utils.invalidate_drafts_for_path(repo_root, rel)
        draft_utils.remove_draft(repo_root, state_key)
    except Exception as exc:
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
    "contextspace_dir",
    "contextspace_doc_path",
    "normalize_contextspace_doc_kind",
    "read_contextspace_doc",
    "write_contextspace_doc",
]
