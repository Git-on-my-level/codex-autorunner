from __future__ import annotations

import json
import signal
import time
import uuid
from pathlib import Path

from typer.testing import CliRunner

from codex_autorunner.cli import app
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_reaper import (
    inspect_flow_workers,
    reap_stale_flow_workers,
)
from codex_autorunner.core.time_utils import now_iso


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".codex-autorunner" / "flows").mkdir(parents=True)
    return repo


def _write_worker(repo: Path, run_id: str, pid: int, spawned_at: float) -> None:
    run_dir = repo / ".codex-autorunner" / "flows" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "worker.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "cmd": ["python", "-m", "codex_autorunner", "flow", "worker"],
                "repo_root": str(repo),
                "spawned_at": spawned_at,
            }
        ),
        encoding="utf-8",
    )


def _create_run(repo: Path, run_id: str, status: FlowRunStatus) -> None:
    with FlowStore(repo / ".codex-autorunner" / "flows.db") as store:
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={},
            current_step="run",
        )
        store.update_flow_run_status(
            run_id,
            status=status,
            started_at=now_iso(),
            finished_at="2026-04-30T00:00:00Z" if status.is_terminal() else None,
        )


def test_inspect_flow_workers_classifies_active_stale_and_zombie(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    active_id = str(uuid.uuid4())
    stale_id = str(uuid.uuid4())
    zombie_id = str(uuid.uuid4())
    _create_run(repo, active_id, FlowRunStatus.RUNNING)
    _create_run(repo, stale_id, FlowRunStatus.COMPLETED)
    now = time.time()
    _write_worker(repo, active_id, 111, now)
    _write_worker(repo, stale_id, 222, now)
    _write_worker(repo, zombie_id, 333, now)

    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._pid_is_running",
        lambda pid: True,
    )

    workers = inspect_flow_workers(repo)
    by_run = {worker.run_id: worker for worker in workers}
    assert by_run[active_id].classification == "active"
    assert by_run[stale_id].classification == "stale"
    assert by_run[zombie_id].classification == "zombie"


def test_reap_stale_flow_workers_writes_exit_info_and_signals(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(repo, run_id, FlowRunStatus.COMPLETED)
    _write_worker(repo, run_id, 444, time.time() - 7200)
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._pid_is_running",
        lambda pid: not signals,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._send_signal",
        lambda pid, sig: signals.append((pid, sig)),
    )

    summary = reap_stale_flow_workers(repo, terminate_grace_seconds=0.01)

    assert summary.pruned_count == 1
    assert signals == [(444, signal.SIGTERM)]
    exit_info = json.loads(
        (repo / ".codex-autorunner" / "flows" / run_id / "worker.exit.json").read_text(
            encoding="utf-8"
        )
    )
    assert exit_info["shutdown_intent"] is True
    assert exit_info["returncode"] == -signal.SIGTERM


def test_doctor_flow_workers_json_lists_workers(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(repo, run_id, FlowRunStatus.COMPLETED)
    _write_worker(repo, run_id, 555, time.time())
    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._pid_is_running",
        lambda pid: True,
    )

    result = CliRunner().invoke(
        app,
        ["doctor", "flow-workers", "--repo", str(repo), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stale_count"] == 1
    assert payload["workers"][0]["run_id"] == run_id
