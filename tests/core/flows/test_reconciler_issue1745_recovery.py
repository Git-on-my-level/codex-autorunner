from __future__ import annotations

import json
import signal
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest
from tests.support.git_test_helpers import init_git_repo as _init_git_repo

from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.flows.reconciler import reconcile_flow_runs
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.worker_reaper import reap_stale_flow_workers
from codex_autorunner.core.time_utils import now_iso
from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import TicketRunner


class FakeAgentPool:
    def __init__(self, handler: Callable[[AgentTurnRequest], AgentTurnResult]):
        self._handler = handler
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        return self._handler(req)


def _write_ticket(path: Path, *, done: bool) -> None:
    path.write_text(
        "---\n"
        f"ticket_id: tkt_{uuid.uuid4().hex}\n"
        "agent: codex\n"
        f"done: {str(done).lower()}\n"
        "title: Test\n"
        "---\n\n"
        "Do the thing\n",
        encoding="utf-8",
    )


def _write_worker(repo: Path, run_id: str, pid: int, spawned_at: float) -> None:
    run_dir = repo / ".codex-autorunner" / "flows" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.asyncio
async def test_reconcile_after_stale_reaper_preserves_commit_before_advance_for_done_current_ticket(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path
    _init_git_repo(repo)
    ticket_dir = repo / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    first_ticket = ticket_dir / "TICKET-001.md"
    second_ticket = ticket_dir / "TICKET-002.md"
    _write_ticket(first_ticket, done=True)
    _write_ticket(second_ticket, done=False)
    (repo / "work.txt").write_text("dirty\n", encoding="utf-8")

    run_id = str(uuid.uuid4())
    engine_state = {
        "status": "running",
        "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
        "current_ticket_id": "ticket-1",
    }
    with FlowStore(repo / ".codex-autorunner" / "flows.db") as store:
        store.create_flow_run(
            run_id=run_id,
            flow_type="ticket_flow",
            input_data={"workspace_root": str(repo)},
            state={"ticket_engine": engine_state},
            current_step="ticket_turn",
        )
        store.update_flow_run_status(
            run_id,
            status=FlowRunStatus.RUNNING,
            started_at=now_iso(),
        )

    _write_worker(repo, run_id, 99123, time.time() - 7200)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._pid_is_running",
        lambda pid: not signals,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_reaper._send_signal",
        lambda pid, sig: signals.append((pid, sig)),
    )

    reap_summary = reap_stale_flow_workers(
        repo, max_age_seconds=60.0, terminate_grace_seconds=0.01
    )

    assert reap_summary.pruned_count == 1
    monkeypatch.setattr(
        "codex_autorunner.core.flows.worker_process._pid_is_running",
        lambda pid: False,
    )

    reconcile_result = reconcile_flow_runs(repo)

    assert reconcile_result.summary.updated == 1
    reconciled = reconcile_result.records[0]
    assert reconciled.status == FlowRunStatus.FAILED
    assert reconciled.state["ticket_engine"]["current_ticket"] == (
        ".codex-autorunner/tickets/TICKET-001.md"
    )
    crash_info = json.loads(
        (repo / ".codex-autorunner" / "flows" / run_id / "crash.json").read_text(
            encoding="utf-8"
        )
    )
    assert crash_info["worker_pid"] == 99123
    assert crash_info["exit_code"] == -signal.SIGTERM
    assert crash_info["signal"] == "SIGTERM"
    assert crash_info["exit_origin"] == "stale_reaper"
    assert crash_info["exit_kind"] == "reaped_stale"
    assert crash_info["reap_reason"] == "metadata_age_exceeded"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        assert "<CAR_COMMIT_REQUIRED>" in req.prompt
        assert "TICKET-001.md" in req.prompt
        assert "TICKET-002.md" not in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t1",
            text="commit still pending",
        )

    runner = TicketRunner(
        workspace_root=repo,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    step = await runner.step(reconciled.state["ticket_engine"])

    assert step.status == "continue"
    assert step.state["current_ticket"] == ".codex-autorunner/tickets/TICKET-001.md"
    assert step.state["commit"]["pending"] is True
