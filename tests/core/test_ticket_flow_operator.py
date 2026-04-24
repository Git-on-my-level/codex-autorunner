from __future__ import annotations

import json
from pathlib import Path

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.ticket_flow_operator import (
    build_ticket_flow_operator_service,
    build_ticket_flow_run_state,
    ticket_flow_preflight,
)


def _write_ticket(repo_root: Path, name: str, *, done: bool = False) -> None:
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / name).write_text(
        (
            "---\n"
            'ticket_id: "tkt_operator001"\n'
            "agent: codex\n"
            f"done: {'true' if done else 'false'}\n"
            "---\n"
        ),
        encoding="utf-8",
    )


def _write_dispatch(
    repo_root: Path,
    run_id: str,
    seq: int,
    *,
    mode: str,
    handoff: bool = False,
) -> None:
    entry_dir = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / f"{seq:04d}"
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = ["---", f"mode: {mode}", "title: Example"]
    if handoff:
        frontmatter.append("is_handoff: true")
    frontmatter.append("---")
    (entry_dir / "DISPATCH.md").write_text(
        "\n".join(frontmatter) + "\n\nBody\n",
        encoding="utf-8",
    )


def _write_dead_worker_artifacts(repo_root: Path, run_id: str) -> None:
    artifacts_dir = repo_root / ".codex-autorunner" / "flows" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "worker.json").write_text(
        json.dumps({"pid": 999_999, "cmd": ["python"], "spawned_at": 1.0}),
        encoding="utf-8",
    )
    (artifacts_dir / "crash.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-02-13T14:00:00Z",
                "worker_pid": 999_999,
                "exit_code": 137,
                "signal": "SIGKILL",
                "last_event": "item/reasoning/summaryTextDelta",
                "exception": "RepoNotFoundError: cwd mismatch",
            }
        ),
        encoding="utf-8",
    )


def test_ticket_flow_operator_preflight_reports_no_tickets(tmp_path: Path) -> None:
    report = ticket_flow_preflight(tmp_path, config=None)
    failing = {check.check_id for check in report.checks if check.status == "error"}
    assert "tickets_present" in failing


def test_ticket_flow_operator_latest_dispatch_prefers_handoff_and_turn_summary(
    tmp_path: Path,
) -> None:
    repo_root = Path(tmp_path)
    run_id = "11111111-1111-1111-1111-111111111111"
    _write_dispatch(repo_root, run_id, seq=2, mode="turn_summary")
    _write_dispatch(repo_root, run_id, seq=1, mode="pause", handoff=True)

    operator = build_ticket_flow_operator_service(repo_root)
    latest = operator.latest_dispatch(
        run_id,
        {"workspace_root": str(repo_root), "runs_dir": ".codex-autorunner/runs"},
        include_turn_summary=True,
    )

    assert latest is not None
    assert latest["seq"] == 1
    assert latest["dispatch"]["mode"] == "pause"
    assert latest["turn_summary_seq"] == 2
    assert latest["turn_summary"]["mode"] == "turn_summary"


def test_ticket_flow_operator_marks_stale_paused_dispatch_when_no_tickets_remain(
    tmp_path: Path,
) -> None:
    repo_root = Path(tmp_path)
    (repo_root / ".codex-autorunner" / "tickets").mkdir(parents=True, exist_ok=True)
    operator = build_ticket_flow_operator_service(repo_root)

    has_dispatch, reason = operator.resolve_paused_dispatch_state(
        record_status=FlowRunStatus.PAUSED,
        latest_payload={
            "seq": 1,
            "latest_seq": 2,
            "dispatch": {"mode": "pause", "is_handoff": True},
        },
        latest_reply_seq=0,
    )

    assert has_dispatch is False
    assert reason is not None
    assert "stale" in reason.lower()
    assert "no tickets remain" in reason.lower()


def test_ticket_flow_operator_build_run_state_flags_dead_worker(tmp_path: Path) -> None:
    repo_root = Path(tmp_path)
    _write_ticket(repo_root, "TICKET-001.md")
    run_id = "22222222-2222-2222-2222-222222222222"

    db_path = repo_root / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.initialize()
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={
                "workspace_root": str(repo_root),
                "runs_dir": ".codex-autorunner/runs",
            },
            metadata={},
            state={},
            current_step="ticket_turn",
        )
        store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
        record = store.get_flow_run(run_id)
        assert record is not None

        _write_dead_worker_artifacts(repo_root, run_id)
        run_state = build_ticket_flow_run_state(
            repo_root=repo_root,
            repo_id="repo",
            record=record,
            store=store,
            has_pending_dispatch=False,
        )

    assert run_state["state"] == "dead"
    assert run_state["worker_status"] == "dead_unexpected"
    assert "Worker not running" in (run_state.get("blocking_reason") or "")
    assert run_state["crash"]["summary"]
