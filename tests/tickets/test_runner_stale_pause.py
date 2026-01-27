from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import TicketRunner


class FakeAgentPool:
    def __init__(self, ticket_path: Path):
        self.ticket_path = ticket_path

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        # Mark ticket done to allow runner to advance
        raw = self.ticket_path.read_text(encoding="utf-8")
        self.ticket_path.write_text(
            raw.replace("done: false", "done: true"), encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="turned",
        )


@pytest.mark.asyncio
async def test_runner_clears_stale_pause_and_runs(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\ntitle: T\n---\n\nbody\n",
        encoding="utf-8",
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            runs_dir=Path(".codex-autorunner/runs"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(ticket_path),
    )

    state = {"status": "paused", "reason": "stale", "ticket_turns": 0}
    result = await runner.step(state)

    assert result.status == "continue"
    assert result.state.get("status") == "running"
    assert result.state.get("reason") is None
    assert result.state.get("reason_details") is None
