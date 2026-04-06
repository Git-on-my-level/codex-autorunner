from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, TypedDict

from ..bootstrap import pma_active_context_content, pma_docs_dir
from .utils import atomic_write

logger = logging.getLogger(__name__)

PMA_ACTIVE_CONTEXT_STATE_FILENAME = ".active_context_state.json"
PMA_ACTIVE_CONTEXT_MAX_LINES = 200


class ActiveContextState(TypedDict, total=False):
    version: int
    last_auto_pruned_at: str
    line_count_before: int
    line_budget: int


class ActiveContextAutoPruneMeta(TypedDict):
    last_auto_pruned_at: str
    line_count_before: int
    line_budget: int


def _active_context_state_path(hub_root: Path) -> Path:
    return pma_docs_dir(hub_root) / PMA_ACTIVE_CONTEXT_STATE_FILENAME


def _coerce_active_context_state(payload: object) -> ActiveContextState:
    if not isinstance(payload, Mapping):
        return {}
    state: ActiveContextState = {}
    version = payload.get("version")
    if isinstance(version, int):
        state["version"] = version
    last_pruned_at = payload.get("last_auto_pruned_at")
    if isinstance(last_pruned_at, str) and last_pruned_at.strip():
        state["last_auto_pruned_at"] = last_pruned_at.strip()
    line_count_before = payload.get("line_count_before")
    if isinstance(line_count_before, int):
        state["line_count_before"] = line_count_before
    line_budget = payload.get("line_budget")
    if isinstance(line_budget, int):
        state["line_budget"] = line_budget
    return state


def _load_active_context_state(hub_root: Path) -> ActiveContextState:
    path = _active_context_state_path(hub_root)
    try:
        raw = path.read_text(encoding="utf-8")
        return _coerce_active_context_state(json.loads(raw))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load active context state: %s", exc)
    return {}


def _save_active_context_state(hub_root: Path, payload: ActiveContextState) -> None:
    path = _active_context_state_path(hub_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def get_active_context_auto_prune_meta(
    hub_root: Path,
) -> Optional[ActiveContextAutoPruneMeta]:
    payload = _load_active_context_state(hub_root)
    if not payload:
        return None
    last_at = payload.get("last_auto_pruned_at")
    line_before = payload.get("line_count_before")
    line_budget = payload.get("line_budget")
    if not isinstance(last_at, str) or not last_at.strip():
        return None
    if not isinstance(line_before, int):
        line_before = 0
    if not isinstance(line_budget, int):
        line_budget = PMA_ACTIVE_CONTEXT_MAX_LINES
    return {
        "last_auto_pruned_at": last_at.strip(),
        "line_count_before": line_before,
        "line_budget": line_budget,
    }


def maybe_auto_prune_active_context(
    hub_root: Path,
    *,
    max_lines: int,
) -> Optional[ActiveContextState]:
    try:
        parsed_max_lines = int(max_lines)
    except (TypeError, ValueError):
        parsed_max_lines = PMA_ACTIVE_CONTEXT_MAX_LINES
    max_lines = (
        parsed_max_lines if parsed_max_lines > 0 else PMA_ACTIVE_CONTEXT_MAX_LINES
    )
    docs_dir = pma_docs_dir(hub_root)
    active_context_path = docs_dir / "active_context.md"
    context_log_path = docs_dir / "context_log.md"
    try:
        active_content = active_context_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read active context file: %s", exc)
        return None
    line_count = len(active_content.splitlines())
    if line_count <= max_lines:
        return None

    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot_header = f"\n\n## Snapshot: {timestamp}\n\n"
    snapshot_content = snapshot_header + active_content

    try:
        with context_log_path.open("a", encoding="utf-8") as f:
            f.write(snapshot_content)
    except OSError as exc:
        logger.warning("Could not write to context log: %s", exc)
        return None

    pruned_content = (
        f"{pma_active_context_content().rstrip()}\n\n"
        f"> Auto-pruned on {timestamp} (had {line_count} lines; budget: {max_lines}).\n"
    )
    try:
        atomic_write(active_context_path, pruned_content)
    except OSError as exc:
        logger.warning("Could not write pruned active context: %s", exc)
        return None

    state: ActiveContextState = {
        "version": 1,
        "last_auto_pruned_at": timestamp,
        "line_count_before": line_count,
        "line_budget": max_lines,
    }
    try:
        _save_active_context_state(hub_root, state)
    except OSError as exc:
        logger.warning("Could not save auto-prune state: %s", exc)
    return state


__all__ = [
    "PMA_ACTIVE_CONTEXT_MAX_LINES",
    "PMA_ACTIVE_CONTEXT_STATE_FILENAME",
    "ActiveContextAutoPruneMeta",
    "ActiveContextState",
    "get_active_context_auto_prune_meta",
    "maybe_auto_prune_active_context",
]
