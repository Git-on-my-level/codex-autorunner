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
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "review.md").write_text("Please review", encoding="utf-8")
        dispatch_path.write_text(
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
    assert r1.dispatch.dispatch.mode == "pause"
    # dispatch_seq is 2: dispatch at seq=1, turn_summary at seq=2
    assert r1.state.get("dispatch_seq") == 2
    assert (run_dir / "dispatch_history" / "0001" / "DISPATCH.md").exists()
    assert (run_dir / "dispatch_history" / "0001" / "review.md").exists()
    # Turn summary should also be created
    assert (run_dir / "dispatch_history" / "0002" / "DISPATCH.md").exists()


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


@pytest.mark.asyncio
async def test_ticket_runner_switches_agents_between_tickets(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_1 = ticket_dir / "TICKET-001.md"
    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_1, agent="codex", done=False)
    _write_ticket(ticket_2, agent="opencode", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.agent_id == "codex":
            _set_ticket_done(ticket_1, done=True)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv-codex",
                turn_id="t1",
                text="codex turn",
            )
        _set_ticket_done(ticket_2, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-opencode",
            turn_id="t2",
            text="opencode turn",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    r2 = await runner.step(r1.state)
    r3 = await runner.step(r2.state)

    assert r1.status == "continue"
    assert r2.status == "continue"
    assert r3.status == "completed"
    assert [req.agent_id for req in pool.requests] == ["codex", "opencode"]
    assert pool.requests[0].conversation_id is None
    assert pool.requests[1].conversation_id is None
    assert r1.agent_conversation_id == "conv-codex"
    assert r2.agent_conversation_id == "conv-opencode"
    assert r1.current_ticket.endswith("TICKET-001.md")
    assert r2.current_ticket.endswith("TICKET-002.md")


@pytest.mark.asyncio
async def test_ticket_runner_advances_through_multiple_tickets(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    tickets = []
    for idx in range(1, 4):
        ticket_path = ticket_dir / f"TICKET-00{idx}.md"
        _write_ticket(ticket_path, done=False)
        tickets.append(ticket_path)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        path = tickets[len(pool.requests) - 1]
        _set_ticket_done(path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=f"conv-{len(pool.requests)}",
            turn_id=f"t{len(pool.requests)}",
            text=f"done-{path.name}",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    r2 = await runner.step(r1.state)
    r3 = await runner.step(r2.state)
    r4 = await runner.step(r3.state)

    assert [r.status for r in (r1, r2, r3, r4)] == [
        "continue",
        "continue",
        "continue",
        "completed",
    ]
    assert [r.current_ticket for r in (r1, r2, r3)] == [
        str(Path(".codex-autorunner/tickets/TICKET-001.md")),
        str(Path(".codex-autorunner/tickets/TICKET-002.md")),
        str(Path(".codex-autorunner/tickets/TICKET-003.md")),
    ]
    assert pool.requests[0].agent_id == "codex"
    assert pool.requests[1].agent_id == "codex"
    assert pool.requests[2].agent_id == "codex"
    assert pool.requests[0].conversation_id is None
    assert pool.requests[1].conversation_id is None
    assert pool.requests[2].conversation_id is None


@pytest.mark.asyncio
async def test_ticket_runner_notify_does_not_pause_and_pause_does(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        turn = len(pool.requests)
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        mode = "notify" if turn == 1 else "pause"
        dispatch_path.write_text(
            f"---\nmode: {mode}\n---\n\nTurn {turn}\n", encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=f"conv-{turn}",
            turn_id=f"t{turn}",
            text=f"turn-{turn}",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=runs_dir,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    r2 = await runner.step(r1.state)

    dispatch_history = run_dir / "dispatch_history"
    assert r1.status == "continue"
    assert r1.dispatch is not None
    assert r1.dispatch.dispatch.mode == "notify"
    assert (dispatch_history / "0001" / "DISPATCH.md").exists()
    # dispatch_seq is 2: dispatch at seq=1, turn_summary at seq=2
    assert r1.state.get("dispatch_seq") == 2
    # Turn summary should also be created
    assert (dispatch_history / "0002" / "DISPATCH.md").exists()

    assert r2.status == "paused"
    assert r2.dispatch is not None
    assert r2.dispatch.dispatch.mode == "pause"
    # dispatch_seq is 4: previous was 2, dispatch at seq=3, turn_summary at seq=4
    assert (dispatch_history / "0003" / "DISPATCH.md").exists()
    assert r2.state.get("dispatch_seq") == 4
    # Turn summary should also be created
    assert (dispatch_history / "0004" / "DISPATCH.md").exists()


@pytest.mark.asyncio
async def test_ticket_runner_consumes_reply_history(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    reply_history = workspace_root / runs_dir / run_id / "reply_history"
    (reply_history / "0001").mkdir(parents=True, exist_ok=True)
    (reply_history / "0001" / "USER_REPLY.md").write_text(
        "---\ntitle: First\n---\n\nFirst reply\n", encoding="utf-8"
    )

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        turn = len(pool.requests)
        if turn == 1:
            assert "[USER_REPLY 0001]" in req.prompt
            assert "First reply" in req.prompt
        else:
            assert "[USER_REPLY 0001]" not in req.prompt
            assert "[USER_REPLY 0002]" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=f"conv-{turn}",
            turn_id=f"t{turn}",
            text=f"turn-{turn}",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=runs_dir,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    # Add a second reply after the first turn to ensure sequencing.
    (reply_history / "0002").mkdir(parents=True, exist_ok=True)
    (reply_history / "0002" / "USER_REPLY.md").write_text(
        "---\ntitle: Second\n---\n\nSecond reply\n", encoding="utf-8"
    )
    r2 = await runner.step(r1.state)

    assert r1.state.get("reply_seq") == 1
    assert r2.state.get("reply_seq") == 2
    assert len(pool.requests) == 2


@pytest.mark.asyncio
async def test_ticket_runner_resumes_after_requires_created(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, requires=["SPEC.md"], done=False)

    spec_path = workspace_root / "SPEC.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-1",
            turn_id="t1",
            text="processed after spec",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    paused = await runner.step({})
    assert paused.status == "paused"
    assert not pool.requests
    assert "Missing required input files" in (paused.reason or "")

    spec_path.write_text("# Spec\n", encoding="utf-8")
    resumed = await runner.step(paused.state)
    completed = await runner.step(resumed.state)

    assert len(pool.requests) == 1
    assert resumed.status == "continue"
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_ticket_runner_resolves_ticket_requires_relative_to_ticket_dir(
    tmp_path: Path,
) -> None:
    """Test that requires can reference other tickets by filename only.

    When a ticket has `requires: [TICKET-001.md]`, the runner should find
    the file at `.codex-autorunner/tickets/TICKET-001.md` even though only
    the filename is specified (not the full relative path).
    """
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    # Create TICKET-001.md (marked as done)
    ticket_001 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_001, done=True)

    # Create TICKET-002.md that requires TICKET-001.md by filename only
    ticket_002 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_002, requires=["TICKET-001.md"])

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        _set_ticket_done(ticket_002, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-1",
            turn_id="t1",
            text="processed ticket 2",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # Should NOT pause for missing requires since TICKET-001.md exists in ticket_dir
    result = await runner.step({})
    assert (
        result.status == "continue"
    ), f"Expected continue, got {result.status}: {result.reason}"
    assert len(pool.requests) == 1

    # Complete the flow
    completed = await runner.step(result.state)
    assert completed.status == "completed"
