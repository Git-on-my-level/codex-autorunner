from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.pause_dispatch import (
    latest_dispatch_seq,
    list_unseen_ticket_flow_dispatches,
    load_latest_paused_ticket_flow_dispatch,
)
from codex_autorunner.core.flows.store import FlowStore


def _init_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    seed_hub_files(repo_root, force=True)
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


def test_load_latest_paused_ticket_flow_dispatch_prefers_pause_over_turn_summary(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(repo_root, run_id="run-3")

    history_root = (
        repo_root / ".codex-autorunner" / "runs" / "run-3" / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Need input\n---\n\nPlease confirm the rollout plan.\n",
        encoding="utf-8",
    )
    (history_root / "0002").mkdir(parents=True)
    (history_root / "0002" / "DISPATCH.md").write_text(
        "---\nmode: turn_summary\n---\n\nFinal turn summary that should not be sent.\n",
        encoding="utf-8",
    )

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-3"
    assert snapshot.dispatch_seq == "0001"
    assert (
        snapshot.dispatch_markdown == "Need input\n\nPlease confirm the rollout plan."
    )
    assert snapshot.dispatch_dir == history_root / "0001"


def test_load_latest_paused_ticket_flow_dispatch_ignores_older_non_utf8_history(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(repo_root, run_id="run-utf8")

    history_root = (
        repo_root / ".codex-autorunner" / "runs" / "run-utf8" / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_bytes(b"\xff\xfe\x00bad")
    (history_root / "0002").mkdir(parents=True)
    (history_root / "0002" / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Need input\n---\n\nPlease use the latest valid dispatch.\n",
        encoding="utf-8",
    )
    (history_root / "0003").mkdir(parents=True)
    (history_root / "0003" / "DISPATCH.md").write_text(
        "---\nmode: turn_summary\n---\n\nLatest summary should not hide the pause.\n",
        encoding="utf-8",
    )

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-utf8"
    assert snapshot.dispatch_seq == "0002"
    assert snapshot.allow_resume_hint is True
    assert (
        snapshot.dispatch_markdown
        == "Need input\n\nPlease use the latest valid dispatch."
    )
    assert snapshot.dispatch_dir == history_root / "0002"


def test_load_latest_paused_ticket_flow_dispatch_fails_closed_for_latest_invalid_dispatch(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(repo_root, run_id="run-invalid")

    history_root = (
        repo_root / ".codex-autorunner" / "runs" / "run-invalid" / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Older prompt\n---\n\nThis should not be surfaced.\n",
        encoding="utf-8",
    )
    (history_root / "0002").mkdir(parents=True)
    (history_root / "0002" / "DISPATCH.md").write_text(
        "---\nmode: broken\n---\n\nMalformed latest dispatch.\n",
        encoding="utf-8",
    )

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-invalid"
    assert snapshot.dispatch_seq == "0002"
    assert snapshot.allow_resume_hint is False
    assert (
        snapshot.dispatch_markdown
        == "Latest paused dispatch #0002 is unreadable or invalid.\n\n"
        "Errors:\n"
        "- frontmatter.mode must be 'notify', 'pause', or 'turn_summary'.\n\n"
        "Fix DISPATCH.md for that paused turn before resuming."
    )
    assert snapshot.dispatch_dir == history_root / "0002"


def test_load_latest_paused_ticket_flow_dispatch_reports_unreadable_latest_dispatch(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(repo_root, run_id="run-missing")

    history_root = (
        repo_root / ".codex-autorunner" / "runs" / "run-missing" / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)

    snapshot = load_latest_paused_ticket_flow_dispatch(repo_root)
    assert snapshot is not None
    assert snapshot.run_id == "run-missing"
    assert snapshot.dispatch_seq == "0001"
    assert snapshot.allow_resume_hint is False
    assert "Latest paused dispatch #0001 is unreadable or invalid." in (
        snapshot.dispatch_markdown
    )
    assert "Failed to read dispatch file:" in snapshot.dispatch_markdown
    assert "Fix DISPATCH.md for that paused turn before resuming." in (
        snapshot.dispatch_markdown
    )
    assert snapshot.dispatch_dir == history_root / "0001"


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


def test_list_unseen_ticket_flow_dispatches_falls_back_when_only_turn_summary_exists(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(
        repo_root,
        run_id="run-summary-only",
        state={"ticket_engine": {"reason": "Investigate the last stalled turn."}},
    )

    history_root = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / "run-summary-only"
        / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_text(
        "---\nmode: turn_summary\n---\n\nInternal summary that should not be sent.\n",
        encoding="utf-8",
    )

    snapshots = list_unseen_ticket_flow_dispatches(repo_root)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.run_id == "run-summary-only"
    assert snapshot.dispatch_seq == "0001"
    assert snapshot.dispatch_markdown == "Reason: Investigate the last stalled turn."
    assert snapshot.dispatch_dir is None
    assert snapshot.mode == "pause"
    assert snapshot.is_handoff is True
    assert snapshot.allow_resume_hint is True


def test_list_unseen_ticket_flow_dispatches_does_not_repeat_after_seen_pause_then_turn_summary(
    tmp_path: Path,
) -> None:
    repo_root = _init_repo(tmp_path)
    _create_paused_run(
        repo_root,
        run_id="run-summary-after-pause",
        state={"ticket_engine": {"reason": "Need confirmation before continuing."}},
    )

    history_root = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / "run-summary-after-pause"
        / "dispatch_history"
    )
    (history_root / "0001").mkdir(parents=True)
    (history_root / "0001" / "DISPATCH.md").write_text(
        "---\nmode: pause\ntitle: Need input\n---\n\nPlease confirm the plan.\n",
        encoding="utf-8",
    )
    (history_root / "0002").mkdir(parents=True)
    (history_root / "0002" / "DISPATCH.md").write_text(
        "---\nmode: turn_summary\n---\n\nSummary that should not trigger another notice.\n",
        encoding="utf-8",
    )

    snapshots = list_unseen_ticket_flow_dispatches(
        repo_root,
        last_run_id="run-summary-after-pause",
        last_dispatch_seq="0001",
    )
    assert snapshots == []


def test_latest_dispatch_seq_ignores_non_numeric_entries(tmp_path: Path) -> None:
    history = tmp_path / "dispatch_history"
    history.mkdir()
    (history / "0002").mkdir()
    (history / "0001").mkdir()
    (history / ".hidden").mkdir()
    (history / "abc").mkdir()

    assert latest_dispatch_seq(history) == "0002"
