from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codex_autorunner.tickets.files import read_ticket
from codex_autorunner.tickets.models import TicketRunConfig
from codex_autorunner.tickets.runner import (
    CAR_HUD_MAX_CHARS,
    CAR_HUD_MAX_LINES,
)
from codex_autorunner.tickets.runner_prompt import build_prompt


def test_ticket_flow_prompt_boundaries(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    ticket_path.write_text(
        "---\nagent: codex\ndone: false\n---\nGoal: Test boundaries\n",
        encoding="utf-8",
    )

    outbox_paths = MagicMock()
    outbox_paths.dispatch_dir = (
        workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
    )
    outbox_paths.dispatch_path = (
        workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
    )

    ticket_doc, _ = read_ticket(ticket_path)
    prompt = build_prompt(
        ticket_path=ticket_path,
        workspace_root=workspace_root,
        ticket_doc=ticket_doc,
        last_agent_output=None,
        outbox_paths=outbox_paths,
        lint_errors=None,
        prompt_max_bytes=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ).prompt_max_bytes,
    )

    assert "<CAR_TICKET_FLOW_PROMPT>" in prompt
    assert "</CAR_TICKET_FLOW_PROMPT>" in prompt
    assert "<CAR_HUD>" in prompt
    assert "</CAR_HUD>" in prompt
    assert "<CAR_CURRENT_TICKET_FILE>" in prompt
    assert "</CAR_CURRENT_TICKET_FILE>" in prompt
    assert "<TICKET_MARKDOWN>" in prompt
    assert "</TICKET_MARKDOWN>" in prompt

    hud_start = prompt.index("<CAR_HUD>") + len("<CAR_HUD>\n")
    hud_end = prompt.index("</CAR_HUD>")
    hud = prompt[hud_start:hud_end].rstrip("\n")
    assert len(hud) <= CAR_HUD_MAX_CHARS
    assert len(hud.splitlines()) <= CAR_HUD_MAX_LINES
    assert "car describe --json" in hud

    start = prompt.index("<CAR_CURRENT_TICKET_FILE>")
    end = prompt.index("</CAR_CURRENT_TICKET_FILE>")
    section = prompt[start:end]
    path_marker = "PATH: .codex-autorunner/tickets/TICKET-001.md"
    assert path_marker in section
