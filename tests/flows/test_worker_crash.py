from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_run
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_process import (
    read_worker_crash_info,
    write_worker_crash_info,
)


def test_write_worker_crash_info_roundtrip(tmp_path: Path) -> None:
    repo_root = tmp_path
    run_id = "123e4567-e89b-12d3-a456-426614174000"

    crash_path = write_worker_crash_info(
        repo_root,
        run_id,
        worker_pid=4321,
        exit_code=-9,
        last_event="item/reasoning/summaryTextDelta",
        stderr_tail="",
        exception="RepoNotFoundError: cwd mismatch",
        stack_trace="Traceback ...",
    )
    assert crash_path is not None
    assert crash_path.exists()

    payload = read_worker_crash_info(repo_root, run_id)
    assert payload is not None
    assert payload["worker_pid"] == 4321
    assert payload["exit_code"] == -9
    assert payload["signal"] == "SIGKILL"
    assert payload["last_event"] == "item/reasoning/summaryTextDelta"
    assert payload["exception"] == "RepoNotFoundError: cwd mismatch"
    assert payload["stack_trace"] == "Traceback ..."


def test_write_worker_crash_info_derives_signal_name_from_exit_code(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    run_id = "323e4567-e89b-12d3-a456-426614174000"

    crash_path = write_worker_crash_info(
        repo_root,
        run_id,
        worker_pid=5678,
        exit_code=-15,
        last_event="account/rateLimits/updated",
    )
    assert crash_path is not None
    assert crash_path.exists()

    payload = read_worker_crash_info(repo_root, run_id)
    assert payload is not None
    assert payload["worker_pid"] == 5678
    assert payload["exit_code"] == -15
    assert payload["signal"] == "SIGTERM"
    assert payload["last_event"] == "account/rateLimits/updated"


def test_reconcile_paused_dead_worker_creates_crash_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = tmp_path
    run_id = "223e4567-e89b-12d3-a456-426614174000"
    db = repo_root / ".codex-autorunner" / "flows.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db)
    store.initialize()

    store.create_flow_run(
        run_id=run_id,
        flow_type="ticket_flow",
        input_data={
            "workspace_root": str(repo_root),
            "runs_dir": ".codex-autorunner/runs",
        },
        state={"ticket_engine": {"status": "paused", "current_ticket": "TICKET-001"}},
    )
    store.update_flow_run_status(
        run_id=run_id,
        status=FlowRunStatus.PAUSED,
        state={"ticket_engine": {"status": "paused", "current_ticket": "TICKET-001"}},
    )

    def _fake_health(_repo_root: Path, _run_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            is_alive=False,
            status="dead",
            pid=9876,
            message="worker PID not running",
            exit_code=137,
            stderr_tail="",
            artifact_path=repo_root
            / ".codex-autorunner"
            / "flows"
            / run_id
            / "worker.json",
            crash_info=None,
        )

    monkeypatch.setattr(
        "codex_autorunner.core.flows.reconciler.check_worker_health", _fake_health
    )

    current = store.get_flow_run(run_id)
    assert current is not None
    recovered, updated, locked = reconcile_flow_run(repo_root, current, store)

    assert recovered.status == FlowRunStatus.PAUSED
    assert updated is False
    assert locked is False

    crash_path = repo_root / ".codex-autorunner" / "flows" / run_id / "crash.json"
    assert crash_path.exists()
    crash_payload = json.loads(crash_path.read_text(encoding="utf-8"))
    assert crash_payload["worker_pid"] == 9876
    assert crash_payload["exit_code"] == 137

    dispatch_path = (
        repo_root
        / ".codex-autorunner"
        / "runs"
        / run_id
        / "dispatch_history"
        / "0001"
        / "DISPATCH.md"
    )
    assert dispatch_path.exists()
    dispatch_raw = dispatch_path.read_text(encoding="utf-8")
    assert "Worker crashed" in dispatch_raw
    assert f".codex-autorunner/flows/{run_id}/crash.json" in dispatch_raw

    artifacts = store.get_artifacts(run_id)
    assert any(artifact.kind == "worker_crash" for artifact in artifacts)
