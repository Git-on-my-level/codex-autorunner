from __future__ import annotations

import os
import time
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.reconciler import (
    _mtime_cache,
    _reconcile_skip_signature,
    _record_reconcile_mtime,
    _should_skip_reconcile,
    reconcile_flow_runs,
)
from codex_autorunner.core.flows.store import FlowStore


def test_count_active_flow_runs_returns_zero_for_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    assert store.count_active_flow_runs() == 0
    store.close()


def test_count_active_flow_runs_counts_running_and_paused(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run("r1", "ticket_flow", {})
    store.update_flow_run_status("r1", FlowRunStatus.RUNNING)
    store.create_flow_run("r2", "ticket_flow", {})
    store.update_flow_run_status("r2", FlowRunStatus.PAUSED)
    store.create_flow_run("r3", "ticket_flow", {})
    store.update_flow_run_status("r3", FlowRunStatus.COMPLETED)
    assert store.count_active_flow_runs() == 2
    assert store.count_active_flow_runs(flow_type="ticket_flow") == 2
    store.close()


def test_count_active_flow_runs_filters_by_type(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run("r1", "ticket_flow", {})
    store.update_flow_run_status("r1", FlowRunStatus.RUNNING)
    store.create_flow_run("r2", "other_flow", {})
    store.update_flow_run_status("r2", FlowRunStatus.RUNNING)
    assert store.count_active_flow_runs() == 2
    assert store.count_active_flow_runs(flow_type="ticket_flow") == 1
    assert store.count_active_flow_runs(flow_type="other_flow") == 1
    store.close()


def test_mtime_skip_returns_false_when_no_cache(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    sig = _reconcile_skip_signature(store)
    store.close()
    _mtime_cache.clear()
    assert _should_skip_reconcile(db_path, sig) is False


def test_mtime_skip_returns_false_when_mtime_changes(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    _record_reconcile_mtime(db_path, _reconcile_skip_signature(store))
    store.close()
    time.sleep(0.05)
    os.utime(db_path, None)
    store2 = FlowStore(db_path)
    store2.initialize()
    sig2 = _reconcile_skip_signature(store2)
    store2.close()
    assert _should_skip_reconcile(db_path, sig2) is False


def test_mtime_skip_returns_true_when_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    sig = _reconcile_skip_signature(store)
    _record_reconcile_mtime(db_path, sig)
    assert _should_skip_reconcile(db_path, _reconcile_skip_signature(store)) is True
    store.close()


def test_reconcile_skips_when_db_unchanged_and_no_active(tmp_path: Path) -> None:
    _mtime_cache.clear()
    repo_root = tmp_path
    codex_dir = repo_root / ".codex-autorunner"
    codex_dir.mkdir(parents=True, exist_ok=True)
    db_path = codex_dir / "flows.db"
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run("r1", "ticket_flow", {})
    store.update_flow_run_status("r1", FlowRunStatus.COMPLETED)
    store.close()

    result = reconcile_flow_runs(repo_root)
    assert result.summary.active == 0
    assert len(result.records) > 0

    result2 = reconcile_flow_runs(repo_root)
    assert result2.summary.active == 0
    assert len(result2.records) == 0


def test_reconcile_does_not_skip_when_active_runs_exist(tmp_path: Path) -> None:
    _mtime_cache.clear()
    repo_root = tmp_path
    codex_dir = repo_root / ".codex-autorunner"
    codex_dir.mkdir(parents=True, exist_ok=True)
    db_path = codex_dir / "flows.db"
    store = FlowStore(db_path)
    store.initialize()
    store.create_flow_run("r1", "ticket_flow", {})
    store.update_flow_run_status("r1", FlowRunStatus.RUNNING)
    store.close()

    result = reconcile_flow_runs(repo_root)
    assert result.summary.active >= 1
    assert len(result.records) >= 1

    store3 = FlowStore(db_path)
    store3.initialize()
    _record_reconcile_mtime(db_path, _reconcile_skip_signature(store3))
    store3.close()
    result2 = reconcile_flow_runs(repo_root)
    assert result2.summary.active >= 1
    assert len(result2.records) >= 1
