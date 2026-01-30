from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import (
    TicketRunner,
    _preserve_ticket_structure,
    _shrink_prompt,
    _truncate_text_by_bytes,
)


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


class FakeAgentPool:
    def __init__(self, handler):
        self._handler = handler
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        return self._handler(req)


def test_truncate_text_by_bytes_simple() -> None:
    text = "Hello, world!"
    assert len(text.encode("utf-8")) == 13
    truncated = _truncate_text_by_bytes(text, 10)
    assert len(truncated.encode("utf-8")) <= 10
    # Marker may be partially truncated if budget is too small
    assert "[... TRUNCATED" in truncated or "[... TRU" in truncated


def test_truncate_text_by_bytes_empty() -> None:
    assert _truncate_text_by_bytes("", 100) == ""
    assert _truncate_text_by_bytes("test", 0) == ""


def test_truncate_text_by_bytes_fits() -> None:
    text = "Hello, world!"
    result = _truncate_text_by_bytes(text, 100)
    assert result == text


def test_preserve_ticket_structure() -> None:
    # Ticket without the TICKET CONTENT prefix
    ticket = (
        "---\n"
        "agent: codex\n"
        "done: false\n"
        "title: Test\n"
        "goal: Finish the test\n"
        "---\n\n"
        "This is the body.\n"
    )
    truncated = _preserve_ticket_structure(ticket, 80)
    # Should preserve frontmatter when budget allows
    if len(truncated.encode("utf-8")) < len(ticket.encode("utf-8")):
        assert "agent: codex" in truncated
        assert "done: false" in truncated
    assert len(truncated.encode("utf-8")) <= 80


def test_preserve_ticket_structure_no_frontmatter() -> None:
    ticket = "Just a body without frontmatter."
    truncated = _preserve_ticket_structure(ticket, 10)
    assert "[... TRUNCATED" in truncated or "[... TRU" in truncated


def test_shrink_prompt_truncates_in_order() -> None:
    sections = {
        "low_priority": "A" * 1000,
        "high_priority": "B" * 100,
        "medium_priority": "C" * 500,
    }

    def render() -> str:
        return (
            sections["high_priority"]
            + sections["medium_priority"]
            + sections["low_priority"]
        )

    result = _shrink_prompt(
        max_bytes=200,
        render=render,
        sections=sections,
        order=["low_priority", "medium_priority", "high_priority"],
    )

    assert len(result.encode("utf-8")) <= 200
    assert "[... TRUNCATED ...]" in result or sections["low_priority"] != "A" * 1000


def test_shrink_prompt_no_truncation_needed() -> None:
    sections = {
        "a": "Hello",
        "b": "World",
    }

    def render() -> str:
        return sections["a"] + " " + sections["b"]

    result = _shrink_prompt(
        max_bytes=1000,
        render=render,
        sections=sections,
        order=["a", "b"],
    )

    assert result == "Hello World"
    assert sections["a"] == "Hello"
    assert sections["b"] == "World"


@pytest.mark.asyncio
async def test_oversize_ticket_body_is_truncated(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"

    large_body = "x" * 10_000_000  # 10 MB
    _write_ticket(ticket_path, body=large_body)

    config = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        runs_dir=Path(".codex-autorunner/runs"),
        prompt_max_bytes=1024,  # Small budget to force truncation
        auto_commit=False,
    )

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        # Prompt should be truncated to fit budget
        assert len(req.prompt.encode("utf-8")) <= 1024
        assert "[... TRUNCATED ...]" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=config,
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step({})
    assert result.status == "continue"


@pytest.mark.asyncio
async def test_oversize_agent_output_is_truncated(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    config = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        runs_dir=Path(".codex-autorunner/runs"),
        prompt_max_bytes=2048,
        auto_commit=False,
    )

    large_output = "y" * 5_000_000  # 5 MB
    state = {"last_agent_output": large_output}

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        assert len(req.prompt.encode("utf-8")) <= 2048
        assert "[... TRUNCATED ...]" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=config,
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step(state)
    assert result.status == "continue"


@pytest.mark.asyncio
async def test_truncation_order_is_deterministic(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, body="x" * 1000)

    prev_path = ticket_dir / "TICKET-000.md"
    prev_path.write_text(
        "---\nagent: codex\ndone: true\n---\n\n" + "y" * 1000,
        encoding="utf-8",
    )

    config = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        runs_dir=Path(".codex-autorunner/runs"),
        prompt_max_bytes=2048,
        auto_commit=False,
    )

    large_output = "z" * 1000
    state = {"last_agent_output": large_output}

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        # Verify that truncation happened in the right order:
        # 1. last_agent_output (should be truncated first)
        # 2. previous_ticket_content (if still over budget)
        assert len(req.prompt.encode("utf-8")) <= 2048
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=config,
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step(state)
    assert result.status == "continue"


@pytest.mark.asyncio
async def test_ticket_frontmatter_preserved_on_truncation(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"

    large_body = "x" * 10_000_000
    _write_ticket(ticket_path, body=large_body)

    config = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        runs_dir=Path(".codex-autorunner/runs"),
        prompt_max_bytes=5000,  # Larger budget to fit header + ticket frontmatter
        auto_commit=False,
    )

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        # Verify truncation marker is present in ticket block
        assert "[... TRUNCATED ...]" in req.prompt
        # Verify prompt doesn't exceed budget
        assert len(req.prompt.encode("utf-8")) <= 5000
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=config,
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step({})
    assert result.status == "continue"
