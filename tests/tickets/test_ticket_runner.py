from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import (
    DEFAULT_MAX_TOTAL_TURNS,
    TicketRunConfig,
)
from codex_autorunner.tickets.runner import TicketRunner


def _write_ticket(
    path: Path,
    *,
    agent: str = "codex",
    done: bool = False,
    body: str = "Do the thing",
) -> None:
    text = (
        "---\n"
        f"agent: {agent}\n"
        f"done: {str(done).lower()}\n"
        "title: Test\n"
        "goal: Finish the test\n"
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


def test_ticket_run_config_default_max_turns() -> None:
    cfg = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        runs_dir=Path(".codex-autorunner/runs"),
        auto_commit=False,
    )
    assert cfg.max_total_turns == DEFAULT_MAX_TOTAL_TURNS


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
    await runner.step(r2.state)

    assert len(pool.requests) == 2


async def test_ticket_runner_pauses_on_duplicate_ticket_indices(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    _write_ticket(ticket_dir / "TICKET-001.md", done=False)
    _write_ticket(ticket_dir / "TICKET-001-duplicate.md", done=False)
    _write_ticket(ticket_dir / "TICKET-002.md", done=False)

    runs_dir = workspace_root / ".codex-autorunner" / "runs"
    run_id = "run-1"

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-1",
            turn_id="t1",
            text="done",
        )
    )

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

    result = await runner.step({})

    assert result.status == "paused"
    assert "Duplicate ticket indices" in result.reason
    assert "001" in result.reason_details


@pytest.mark.asyncio
async def test_previous_ticket_context_excluded_by_default(tmp_path: Path) -> None:
    """Test that previous ticket content is NOT included in prompt by default."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    ticket_1 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_1, done=False)
    _set_ticket_done(ticket_1, done=True)

    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_2, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
            include_previous_ticket_context=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert "PREVIOUS TICKET CONTEXT" not in pool.requests[0].prompt


@pytest.mark.asyncio
async def test_previous_ticket_context_included_when_enabled(tmp_path: Path) -> None:
    """Test that previous ticket content IS included (and truncated) when enabled."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    ticket_1 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_1, done=False)
    _set_ticket_done(ticket_1, done=True)

    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_2, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
            include_previous_ticket_context=True,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert "PREVIOUS TICKET CONTEXT (truncated to 16KB" in pool.requests[0].prompt
    assert (
        "Cross-ticket context should flow through contextspace docs"
        in pool.requests[0].prompt
    )
    assert "agent: codex\ndone: true" in pool.requests[0].prompt


def test_is_network_error_detection() -> None:
    """Test network error detection logic."""
    from codex_autorunner.tickets.runner import _is_network_error

    # Positive cases (should return True)
    assert _is_network_error("network error") is True
    assert _is_network_error("Connection timeout") is True
    assert _is_network_error("transport error") is True
    assert _is_network_error("stream disconnected") is True
    assert _is_network_error("Reconnecting... 1/5") is True
    assert _is_network_error("connection refused") is True
    assert _is_network_error("connection reset") is True
    assert _is_network_error("connection broken") is True
    assert _is_network_error("unreachable") is True
    assert _is_network_error("temporary failure") is True

    # Negative cases (should return False)
    assert _is_network_error("validation error") is False
    assert _is_network_error("config error") is False
    assert _is_network_error("permission denied") is False
    assert _is_network_error("auth failed") is False
    assert _is_network_error("") is False
    assert _is_network_error("successful completion") is False


@pytest.mark.asyncio
async def test_ticket_runner_retries_on_network_error(tmp_path: Path) -> None:
    """Test that network errors trigger automatic retries."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0
    max_network_errors = 2

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count <= max_network_errors:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id=f"t{call_count}",
                text="failed",
                error="Network error: stream disconnected",
            )
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id=f"t{call_count}",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error 1, should retry
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert "Network error detected (attempt 1/5)" in r1.reason
    assert r1.state.get("network_retry") is not None
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: network error 2, should retry
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert "Network error detected (attempt 2/5)" in r2.reason
    assert r2.state["network_retry"]["retries"] == 2

    # Third step: success, retry state should be cleared
    r3 = await runner.step(r2.state)
    assert r3.status == "continue"
    assert r3.state.get("network_retry") is None

    assert call_count == 3


@pytest.mark.asyncio
async def test_ticket_runner_pauses_after_network_retries_exhausted(
    tmp_path: Path,
) -> None:
    """Test that the runner pauses when network retries are exhausted."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t1",
            text="failed",
            error="Network error: connection timeout",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            max_network_retries=2,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # Retry 1
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Retry 2
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state["network_retry"]["retries"] == 2

    # Retry 3 (exhausted, should pause)
    r3 = await runner.step(r2.state)
    assert r3.status == "paused"
    assert r3.reason == "Agent turn failed. Fix the issue and resume."
    assert "Network error: connection timeout" in r3.reason_details
    assert r3.state.get("network_retry") is None

    assert len(pool.requests) == 3


@pytest.mark.asyncio
async def test_ticket_runner_clears_network_retry_on_non_network_error(
    tmp_path: Path,
) -> None:
    """Test that network retry state is cleared on non-network errors."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="failed",
                error="Network error: connection failed",
            )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="failed",
            error="Validation error: invalid config",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error, set retry state
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: non-network error, should pause immediately
    r2 = await runner.step(r1.state)
    assert r2.status == "paused"
    assert r2.reason == "Agent turn failed. Fix the issue and resume."
    assert "Validation error" in r2.reason_details
    assert r2.state.get("network_retry") is None


@pytest.mark.asyncio
async def test_ticket_runner_clears_network_retry_on_success(tmp_path: Path) -> None:
    """Test that network retry state is cleared on successful turn."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="failed",
                error="Network error: transport error",
            )
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error, set retry state
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: success, retry state should be cleared
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state.get("network_retry") is None

    assert call_count == 2


@pytest.mark.asyncio
async def test_ticket_runner_archives_user_reply_before_turn(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    run_dir = workspace_root / ".codex-autorunner" / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "USER_REPLY.md").write_text("User says hi\n", encoding="utf-8")

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        assert "User says hi" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="ok",
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

    result = await runner.step({})
    assert result.status == "continue"

    archived_reply = run_dir / "reply_history" / "0001" / "USER_REPLY.md"
    assert archived_reply.exists()
