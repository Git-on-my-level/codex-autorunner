from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import TicketRunner


def _write_ticket(
    path: Path,
    *,
    agent: str = "codex",
    done: bool = False,
    requires: Optional[list[str]] = None,
    body: str = "Do the thing",
) -> None:
    req_block = ""
    if requires:
        req_lines = "\n".join(f"  - {r}" for r in requires)
        req_block = f"requires:\n{req_lines}\n"

    text = (
        "---\n"
        f"agent: {agent}\n"
        f"done: {str(done).lower()}\n"
        "title: Test\n"
        "goal: Finish the test\n"
        f"{req_block}"
        "---\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")


def _set_ticket_done(path: Path, *, done: bool = True) -> None:
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("done: false", f"done: {str(done).lower()}")
    path.write_text(raw, encoding="utf-8")


def _corrupt_ticket_frontmatter(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    # Make 'done' invalid.
    raw = raw.replace("done: false", "done: notabool")
    path.write_text(raw, encoding="utf-8")


class FakeAgentPool:
    def __init__(self, handler: Callable[[AgentTurnRequest], AgentTurnResult]):
        self._handler = handler
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        return self._handler(req)


@pytest.mark.asyncio
async def test_ticket_runner_pauses_when_no_tickets(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(
            lambda req: AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=req.conversation_id or "conv",
                turn_id="t1",
                text="noop",
            )
        ),
    )

    result = await runner.step({})
    assert result.status == "paused"
    assert "No tickets found" in (result.reason or "")


@pytest.mark.asyncio
async def test_ticket_runner_pauses_when_requires_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, requires=["SPEC.md"])

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(
            lambda req: AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=req.conversation_id or "conv",
                turn_id="t1",
                text="noop",
            )
        ),
    )

    result = await runner.step({})
    assert result.status == "paused"
    assert "Missing required input files" in (result.reason or "")


@pytest.mark.asyncio
async def test_ticket_runner_completes_when_all_tickets_done(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    # First step runs agent and marks ticket done.
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state.get("current_ticket") is None

    # Second step should observe all done.
    r2 = await runner.step(r1.state)
    assert r2.status == "completed"
    assert "All tickets done" in (r2.reason or "")


@pytest.mark.asyncio
async def test_ticket_runner_dispatch_pause_message(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    handoff_dir = run_dir / "handoff"
    user_msg = run_dir / "USER_MESSAGE.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        handoff_dir.mkdir(parents=True, exist_ok=True)
        (handoff_dir / "review.md").write_text("Please review", encoding="utf-8")
        user_msg.write_text(
            "---\nmode: pause\n---\n\nReview attached.\n", encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="wrote outbox",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=runs_dir,
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    r1 = await runner.step({})
    assert r1.status == "paused"
    assert r1.dispatch is not None
    assert r1.dispatch.message.mode == "pause"
    assert r1.state.get("outbox_seq") == 1
    assert (run_dir / "handoff_history" / "0001" / "USER_MESSAGE.md").exists()
    assert (run_dir / "handoff_history" / "0001" / "review.md").exists()


@pytest.mark.asyncio
async def test_ticket_runner_lint_retry_reuses_conversation_id(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.conversation_id is None:
            _corrupt_ticket_frontmatter(ticket_path)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="corrupted",
            )

        # Second pass fixes the frontmatter.
        _write_ticket(ticket_path, done=False)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id,
            turn_id="t2",
            text="fixed",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            max_lint_retries=3,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step triggers lint retry (continue, with lint state set).
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert isinstance(r1.state.get("lint"), dict)

    # Second step should pass conversation id + include lint errors in the prompt.
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state.get("lint") is None

    assert len(pool.requests) == 2
    assert pool.requests[1].conversation_id == "conv1"
    assert "Ticket frontmatter lint failed" in pool.requests[1].prompt
