from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.pause_dispatch import (
    latest_dispatch_seq,
    load_latest_paused_ticket_flow_dispatch,
)
from codex_autorunner.core.flows.store import FlowStore


def _init_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_repo_files(repo_root, git_required=False)
    return repo_root


def _create_paused_run(
    repo_root: Path,
    *,
    run_id: str,
    input_data: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data=input_data or {},
            state=state or {},
        )
        store.update_flow_run_status(
            run_id,
            FlowRunStatus.PAUSED,
            state=state if state is not None else {},
            error_message=error_message,
        )


def test_load_latest_paused_ticket_flow_dispatch_reads_latest_history(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(repo_root, run_id="run-1")

    history_root = (
        repo_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_text("older", encoding="utf-8")
    (history_root / "0002").mkdir(parents=True)
    (history_root / "0002" / "DISPATCH.md").write_text("latest", encoding="utf-8")

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-1"
    assert snapshot.dispatch_seq == "0002"
    assert snapshot.dispatch_markdown == "latest"
    assert snapshot.dispatch_dir == history_root / "0002"


def test_load_latest_paused_ticket_flow_dispatch_falls_back_to_reason(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(
        repo_root,
        run_id="run-2",
        state={"ticket_engine": {"reason": "Need user input"}},
    )

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-2"
    assert snapshot.dispatch_seq == "paused"
    assert snapshot.dispatch_markdown == "Reason: Need user input"
    assert snapshot.dispatch_dir is None


def test_latest_dispatch_seq_ignores_non_numeric_entries(tmp_path: Path) -> None:
    history = tmp_path / "dispatch_history"
    history.mkdir()
    (history / "0002").mkdir()
    (history / "0001").mkdir()
    (history / ".hidden").mkdir()
    (history / "abc").mkdir()

    assert latest_dispatch_seq(history) == "0002"
