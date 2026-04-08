"""Automatic telemetry pruning hooks invoked at lifecycle boundaries.

Three integration points:
1. Terminal run completion -- called by the reconciler when a run transitions to a
   terminal status (completed, failed, stopped, superseded).
2. Worktree archive/remove -- called by the worktree manager before final cleanup.
3. Periodic sweep -- iterates all repos known to the hub, including chat-bound
   worktrees that lack teardown cleanup.

All hooks delegate to the shared housekeeping service from ``flow_housekeeping``
so retention logic lives in exactly one place.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from ..config import ConfigError, FlowRetentionConfig, load_repo_config
from .flow_housekeeping import HousekeepResult, execute_housekeep
from .store import FlowStore

_logger = logging.getLogger(__name__)


def _resolve_retention_config(repo_root: Path) -> FlowRetentionConfig:
    try:
        repo_config = load_repo_config(repo_root)
        return getattr(repo_config, "flow_retention", FlowRetentionConfig())
    except (ConfigError, OSError, ValueError, TypeError):
        return FlowRetentionConfig()


def _open_store(repo_root: Path) -> Optional[FlowStore]:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        repo_config = load_repo_config(repo_root)
        durable = bool(getattr(repo_config, "durable_writes", False))
    except (ConfigError, OSError, ValueError, TypeError):
        durable = False
    store = FlowStore(db_path, durable=durable)
    store.initialize()
    return store


def _run_housekeep(
    repo_root: Path,
    *,
    run_ids: Optional[Sequence[str]] = None,
    include_all_terminal: bool = False,
) -> Optional[HousekeepResult]:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    try:
        store = _open_store(repo_root)
    except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
        _logger.warning(
            "flow_housekeep_hook open failed repo=%s: %s", repo_root.name, exc
        )
        return None
    if store is None:
        return None
    try:
        retention = _resolve_retention_config(repo_root)
        result = execute_housekeep(
            repo_root,
            store,
            db_path,
            retention,
            run_ids=run_ids,
            dry_run=False,
            include_all_terminal=include_all_terminal,
        )
        if result.runs_processed > 0:
            _logger.info(
                "flow_housekeep_hook repo=%s runs=%d exported=%d pruned=%d errors=%d",
                repo_root.name,
                result.runs_processed,
                result.events_exported,
                result.events_pruned,
                len(result.errors),
            )
        return result
    except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
        _logger.warning("flow_housekeep_hook failed repo=%s: %s", repo_root.name, exc)
        return None
    finally:
        try:
            store.close()
        except (sqlite3.Error, OSError, ValueError, TypeError):
            pass


def housekeep_on_run_terminal(
    repo_root: Path,
    run_id: str,
) -> Optional[HousekeepResult]:
    """Best-effort housekeeping after a run transitions to terminal status.

    The retention window determines whether the run is actually eligible for
    pruning.  Recently-completed runs will be skipped automatically.
    """
    _logger.debug("housekeep_on_run_terminal repo=%s run=%s", repo_root.name, run_id)
    return _run_housekeep(repo_root, run_ids=[run_id])


def housekeep_on_worktree_cleanup(
    repo_root: Path,
) -> Optional[HousekeepResult]:
    """Housekeeping before worktree archive or removal.

    Targets all terminal runs for the repo because this is the last chance
    to prune before the worktree disappears.
    """
    _logger.info("housekeep_on_worktree_cleanup repo=%s", repo_root.name)
    return _run_housekeep(repo_root, include_all_terminal=True)


@dataclasses.dataclass
class SweepResult:
    repos_scanned: int = 0
    repos_pruned: int = 0
    runs_processed: int = 0
    events_exported: int = 0
    events_pruned: int = 0
    errors: int = 0


def housekeep_sweep_repos(
    repo_roots: Sequence[Path],
) -> SweepResult:
    """Sweep a collection of repo roots, running housekeeping for each.

    Used by the periodic hub sweep to cover all known repos including
    chat-bound worktrees that do not receive teardown cleanup.
    """
    result = SweepResult()
    for repo_root in repo_roots:
        db_path = repo_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            continue
        result.repos_scanned += 1
        hk = _run_housekeep(repo_root)
        if hk is None:
            result.errors += 1
        else:
            if hk.runs_processed > 0:
                result.repos_pruned += 1
            result.runs_processed += hk.runs_processed
            result.events_exported += hk.events_exported
            result.events_pruned += hk.events_pruned
            result.errors += len(hk.errors)
    if result.repos_scanned > 0:
        _logger.info(
            "housekeep_sweep scanned=%d pruned=%d runs=%d exported=%d errors=%d",
            result.repos_scanned,
            result.repos_pruned,
            result.runs_processed,
            result.events_exported,
            result.errors,
        )
    return result


__all__ = [
    "SweepResult",
    "housekeep_on_run_terminal",
    "housekeep_on_worktree_cleanup",
    "housekeep_sweep_repos",
]
