from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from ..bootstrap import ensure_pma_docs, pma_doc_path
from .config import load_hub_config
from .pma_active_context import (
    PMA_ACTIVE_CONTEXT_MAX_LINES,
    ActiveContextAutoPruneMeta,
    get_active_context_auto_prune_meta,
    maybe_auto_prune_active_context,
)
from .pma_context_shared import (
    PMA_CONTEXT_LOG_TAIL_LINES,
    PMA_DOCS_MAX_CHARS,
    _tail_lines,
    _truncate,
)

_logger = logging.getLogger(__name__)


class PmaWorkspaceDocs(TypedDict):
    agents: str
    active_context: str
    active_context_line_count: int
    active_context_max_lines: int
    context_log_tail: str
    active_context_auto_pruned: bool
    active_context_auto_prune: ActiveContextAutoPruneMeta | None


def _read_workspace_doc(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning("Could not read file %s: %s", path, exc)
        return ""


def load_pma_workspace_docs(hub_root: Path) -> PmaWorkspaceDocs:
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        _logger.warning("Could not ensure PMA docs: %s", exc)

    docs_max_chars = PMA_DOCS_MAX_CHARS
    active_context_max_lines = PMA_ACTIVE_CONTEXT_MAX_LINES
    context_log_tail_lines = PMA_CONTEXT_LOG_TAIL_LINES
    try:
        hub_config = load_hub_config(hub_root)
        pma_cfg = getattr(hub_config, "pma", None)
        if pma_cfg is not None:
            docs_max_chars = int(getattr(pma_cfg, "docs_max_chars", docs_max_chars))
            active_context_max_lines = int(
                getattr(pma_cfg, "active_context_max_lines", active_context_max_lines)
            )
            context_log_tail_lines = int(
                getattr(pma_cfg, "context_log_tail_lines", context_log_tail_lines)
            )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _logger.warning("Could not load PMA config: %s", exc)

    auto_prune_state = maybe_auto_prune_active_context(
        hub_root,
        max_lines=active_context_max_lines,
    )
    auto_prune_meta = get_active_context_auto_prune_meta(hub_root)

    agents = _truncate(
        _read_workspace_doc(pma_doc_path(hub_root, "AGENTS.md")),
        docs_max_chars,
    )
    active_context_raw = _read_workspace_doc(
        pma_doc_path(hub_root, "active_context.md")
    )
    active_context_lines = len((active_context_raw or "").splitlines())
    active_context = _truncate(active_context_raw, docs_max_chars)
    context_log_tail = _tail_lines(
        _read_workspace_doc(pma_doc_path(hub_root, "context_log.md")),
        context_log_tail_lines,
    )
    context_log_tail = _truncate(context_log_tail, docs_max_chars)

    return {
        "agents": agents,
        "active_context": active_context,
        "active_context_line_count": active_context_lines,
        "active_context_max_lines": active_context_max_lines,
        "context_log_tail": context_log_tail,
        "active_context_auto_pruned": bool(auto_prune_state),
        "active_context_auto_prune": auto_prune_meta,
    }


def load_pma_prompt(hub_root: Path) -> str:
    path = pma_doc_path(hub_root, "prompt.md")
    try:
        ensure_pma_docs(hub_root)
    except OSError as exc:
        _logger.warning("Could not ensure PMA docs for prompt: %s", exc)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning("Could not read prompt file: %s", exc)
        return ""
