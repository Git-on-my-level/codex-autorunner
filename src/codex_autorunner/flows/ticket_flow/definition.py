from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ...core.flows.definition import EmitEventFn, FlowDefinition, StepOutcome
from ...core.flows.models import FlowEventType, FlowRunRecord
from ...core.utils import find_repo_root
from ...tickets import AgentPool, TicketRunConfig, TicketRunner


def build_ticket_flow_definition(*, agent_pool: AgentPool) -> FlowDefinition:
    """Build the single-step ticket runner flow.

    The flow is intentionally simple: each step executes at most one agent turn
    against the current ticket, and re-schedules itself until paused or complete.
    """

    async def _ticket_turn_step(
        record: FlowRunRecord,
        input_data: Dict[str, Any],
        emit_event: Optional[EmitEventFn],
    ) -> StepOutcome:
        # Namespace all state under `ticket_engine` to avoid collisions with other flows.
        engine_state = (
            record.state.get("ticket_engine")
            if isinstance(record.state, dict)
            else None
        )
        engine_state = dict(engine_state) if isinstance(engine_state, dict) else {}

        repo_root = find_repo_root()
        workspace_root = Path(input_data.get("workspace_root") or repo_root)
        ticket_dir = Path(input_data.get("ticket_dir") or ".codex-autorunner/tickets")
        runs_dir = Path(input_data.get("runs_dir") or ".codex-autorunner/runs")
        max_total_turns = int(input_data.get("max_total_turns") or 25)
        max_lint_retries = int(input_data.get("max_lint_retries") or 3)
        max_commit_retries = int(input_data.get("max_commit_retries") or 2)
        auto_commit = bool(
            input_data.get("auto_commit") if "auto_commit" in input_data else True
        )

        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id=str(record.id),
            config=TicketRunConfig(
                ticket_dir=ticket_dir,
                runs_dir=runs_dir,
                max_total_turns=max_total_turns,
                max_lint_retries=max_lint_retries,
                max_commit_retries=max_commit_retries,
                auto_commit=auto_commit,
            ),
            agent_pool=agent_pool,
        )

        if emit_event is not None:
            emit_event(FlowEventType.STEP_PROGRESS, {"message": "Running ticket turn"})
        result = await runner.step(engine_state, emit_event=emit_event)
        out_state = dict(record.state or {})
        out_state["ticket_engine"] = result.state

        if result.status == "completed":
            return StepOutcome.complete(output=out_state)
        if result.status == "paused":
            return StepOutcome.pause(output=out_state)
        if result.status == "failed":
            return StepOutcome.fail(
                error=result.reason or "Ticket engine failed", output=out_state
            )
        return StepOutcome.continue_to(next_steps={"ticket_turn"}, output=out_state)

    return FlowDefinition(
        flow_type="ticket_flow",
        name="Ticket Flow",
        description="Ticket-based agent workflow runner",
        initial_step="ticket_turn",
        input_schema={
            "type": "object",
            "properties": {
                "workspace_root": {"type": "string"},
                "ticket_dir": {"type": "string"},
                "runs_dir": {"type": "string"},
                "max_total_turns": {"type": "integer"},
                "max_lint_retries": {"type": "integer"},
                "max_commit_retries": {"type": "integer"},
                "auto_commit": {"type": "boolean"},
            },
        },
        steps={"ticket_turn": _ticket_turn_step},
    )
