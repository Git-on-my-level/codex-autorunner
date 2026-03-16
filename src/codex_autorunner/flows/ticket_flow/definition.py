from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ...core.flows.definition import EmitEventFn, FlowDefinition, StepOutcome
from ...core.flows.models import FlowEventType, FlowRunRecord
from ...core.utils import find_repo_root
from ...manifest import ManifestError, load_manifest
from ...tickets import DEFAULT_MAX_TOTAL_TURNS, AgentPool, TicketRunConfig, TicketRunner


def build_ticket_flow_definition(
    *,
    agent_pool: AgentPool,
    auto_commit_default: bool = False,
    include_previous_ticket_context_default: bool = False,
) -> FlowDefinition:
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

        raw_workspace = input_data.get("workspace_root")
        if raw_workspace:
            workspace_root = Path(raw_workspace)
            if not workspace_root.is_absolute():
                raise ValueError("workspace_root must be absolute")
            workspace_root = workspace_root.resolve()
            repo_root = find_repo_root(start=workspace_root)
        else:
            repo_root = find_repo_root()
            workspace_root = repo_root

        ticket_dir = (workspace_root / ".codex-autorunner" / "tickets").resolve()
        max_total_turns = int(
            input_data.get("max_total_turns") or DEFAULT_MAX_TOTAL_TURNS
        )

        repo_id = _resolve_ticket_flow_repo_id(workspace_root)
        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id=str(record.id),
            config=TicketRunConfig(
                ticket_dir=ticket_dir,
                max_total_turns=max_total_turns,
                auto_commit=auto_commit_default,
                include_previous_ticket_context=include_previous_ticket_context_default,
            ),
            agent_pool=agent_pool,
            repo_id=repo_id,
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
                "max_total_turns": {"type": "integer"},
            },
        },
        steps={"ticket_turn": _ticket_turn_step},
    )


def _resolve_ticket_flow_repo_id(workspace_root: Path) -> str:
    current = workspace_root
    for _ in range(5):
        manifest_path = current / ".codex-autorunner" / "manifest.yml"
        if manifest_path.exists():
            try:
                manifest = load_manifest(manifest_path, current)
            except ManifestError:
                return ""
            entry = manifest.get_by_path(current, workspace_root)
            return entry.id if entry else ""
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ""
