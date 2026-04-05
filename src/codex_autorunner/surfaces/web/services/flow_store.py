from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from ....core.flows import FlowRunRecord, FlowRunStatus, FlowStore
from ....core.flows.reconciler import reconcile_flow_run
from ....tickets.files import safe_relpath

_logger = logging.getLogger(__name__)

RequireStore = Callable[[Path], Optional[FlowStore]]
ActiveOrPausedSelector = Callable[[list[FlowRunRecord]], Optional[FlowRunRecord]]


def flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def require_flow_store(
    repo_root: Path, *, logger: Optional[logging.Logger] = None
) -> Optional[FlowStore]:
    log = logger or _logger
    db_path, _ = flow_paths(repo_root)
    store = FlowStore(db_path)
    try:
        store.initialize()
        return store
    except Exception as exc:
        log.warning("Flows database unavailable at %s: %s", db_path, exc)
        return None


def safe_list_flow_runs(
    repo_root: Path,
    flow_type: Optional[str] = None,
    *,
    recover_stuck: bool = False,
    logger: Optional[logging.Logger] = None,
) -> list[FlowRunRecord]:
    log = logger or _logger
    db_path, _ = flow_paths(repo_root)
    store = FlowStore(db_path)
    try:
        store.initialize()
        records = store.list_flow_runs(flow_type=flow_type)
        if recover_stuck:
            records = [
                reconcile_flow_run(repo_root, rec, store, logger=log)[0]
                for rec in records
            ]
        return records
    except Exception as exc:
        log.debug("FlowStore list runs failed: %s", exc)
        return []
    finally:
        try:
            store.close()
        except Exception:
            _logger.debug(
                "Failed to close flow store after listing runs", exc_info=True
            )


def get_flow_record(
    repo_root: Path,
    run_id: str,
    *,
    require_store: RequireStore = require_flow_store,
) -> FlowRunRecord:
    store = require_store(repo_root)
    if store is None:
        raise HTTPException(status_code=503, detail="Flows database unavailable")
    try:
        record = store.get_flow_run(run_id)
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503, detail="Flows database unavailable"
        ) from exc
    finally:
        try:
            store.close()
        except Exception:
            _logger.debug(
                "Failed to close flow store after getting flow record", exc_info=True
            )
    if not record:
        raise HTTPException(status_code=404, detail=f"Flow run {run_id} not found")
    return record


def _active_or_paused_run(records: list[FlowRunRecord]) -> Optional[FlowRunRecord]:
    for record in records:
        if record.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
            return record
    return None


def sync_active_run_current_ticket_paths_after_reorder(
    repo_root: Path,
    renamed_paths: list[tuple[Path, Path]],
    *,
    require_store: RequireStore = require_flow_store,
    active_or_paused_run_selector: ActiveOrPausedSelector = _active_or_paused_run,
    logger: Optional[logging.Logger] = None,
) -> None:
    if not renamed_paths:
        return
    log = logger or _logger
    store = require_store(repo_root)
    if store is None:
        return
    try:
        records = store.list_flow_runs(flow_type="ticket_flow")
        active = active_or_paused_run_selector(records)
        if active is None or not isinstance(active.state, dict):
            return
        rel_map: dict[str, str] = {}
        for old_path, new_path in renamed_paths:
            rel_map[safe_relpath(old_path, repo_root)] = safe_relpath(
                new_path, repo_root
            )
        next_state = dict(active.state)
        changed = False

        current_ticket = next_state.get("current_ticket")
        if isinstance(current_ticket, str):
            updated = rel_map.get(current_ticket)
            if isinstance(updated, str) and updated != current_ticket:
                next_state["current_ticket"] = updated
                changed = True

        ticket_engine = next_state.get("ticket_engine")
        if isinstance(ticket_engine, dict):
            next_ticket_engine = dict(ticket_engine)
            current_ticket = next_ticket_engine.get("current_ticket")
            if isinstance(current_ticket, str):
                updated = rel_map.get(current_ticket)
                if isinstance(updated, str) and updated != current_ticket:
                    next_ticket_engine["current_ticket"] = updated
                    next_state["ticket_engine"] = next_ticket_engine
                    changed = True

        if not changed:
            return
        store.update_flow_run_status(active.id, active.status, state=next_state)
    except Exception as exc:
        log.warning("Failed to sync current_ticket after reorder: %s", exc)
    finally:
        try:
            store.close()
        except Exception as e:
            log.debug("Store close failed: %s", e)
