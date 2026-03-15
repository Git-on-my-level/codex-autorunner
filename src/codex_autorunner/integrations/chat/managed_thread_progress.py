from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    ToolCall,
)
from .progress_primitives import TurnProgressTracker


@dataclass
class ProgressRuntimeState:
    final_message: str = ""
    error_message: str | None = None


@dataclass(frozen=True)
class ProgressTrackerEventOutcome:
    changed: bool
    force: bool = False
    render_mode: str = "live"
    remove_components: bool = False
    terminal: bool = False


def progress_item_id_for_log_line(content: str) -> str | None:
    normalized = " ".join(content.split()).strip().lower()
    if normalized.startswith("tokens used"):
        return "opencode:token-usage"
    if normalized.startswith("context window:"):
        return "opencode:context-window"
    return None


def apply_run_event_to_progress_tracker(
    tracker: TurnProgressTracker,
    run_event: Any,
    *,
    runtime_state: ProgressRuntimeState,
) -> ProgressTrackerEventOutcome:
    if isinstance(run_event, OutputDelta):
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_USER_MESSAGE:
            return ProgressTrackerEventOutcome(changed=False)
        delta = run_event.content
        if not isinstance(delta, str) or not delta.strip():
            return ProgressTrackerEventOutcome(changed=False)
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
            latest_output = tracker.latest_output_text().strip()
            incoming_output = delta.strip()
            if latest_output and (
                incoming_output == latest_output
                or incoming_output.startswith(latest_output)
            ):
                tracker.note_output(delta)
            else:
                tracker.note_output(delta, new_segment=True)
            tracker.end_output_segment()
        elif run_event.delta_type == RUN_EVENT_DELTA_TYPE_LOG_LINE:
            item_id = progress_item_id_for_log_line(delta)
            if item_id:
                if not tracker.update_action_by_item_id(
                    item_id,
                    delta,
                    "update",
                    label="output",
                ):
                    tracker.add_action(
                        "output",
                        delta,
                        "update",
                        item_id=item_id,
                        normalize_text=False,
                    )
            else:
                tracker.note_output(delta, new_segment=True)
                tracker.end_output_segment()
        else:
            tracker.note_output(delta)
        return ProgressTrackerEventOutcome(changed=True)

    if isinstance(run_event, ToolCall):
        tool_name = run_event.tool_name.strip() if run_event.tool_name else ""
        tracker.note_tool(tool_name or "Tool call")
        return ProgressTrackerEventOutcome(changed=True)

    if isinstance(run_event, ApprovalRequested):
        summary = run_event.description.strip() if run_event.description else ""
        tracker.note_approval(summary or "Approval requested")
        return ProgressTrackerEventOutcome(changed=True)

    if isinstance(run_event, RunNotice):
        notice = run_event.message.strip() if run_event.message else ""
        if not notice:
            notice = run_event.kind.strip() if run_event.kind else "notice"
        if run_event.kind in {"thinking", "reasoning"}:
            tracker.note_thinking(notice)
            return ProgressTrackerEventOutcome(changed=True)
        if run_event.kind == "interrupted":
            if tracker.label == "done" and runtime_state.final_message:
                return ProgressTrackerEventOutcome(changed=False)
            runtime_state.error_message = notice or "Turn interrupted"
            tracker.note_error(runtime_state.error_message)
            tracker.clear_transient_action()
            tracker.set_label("cancelled")
            return ProgressTrackerEventOutcome(
                changed=True,
                force=True,
                remove_components=True,
                terminal=True,
            )
        if run_event.kind == "failed":
            if tracker.label == "done" and runtime_state.final_message:
                return ProgressTrackerEventOutcome(changed=False)
            runtime_state.error_message = notice or "Turn failed"
            tracker.note_error(runtime_state.error_message)
            tracker.clear_transient_action()
            tracker.set_label("failed")
            return ProgressTrackerEventOutcome(
                changed=True,
                force=True,
                remove_components=True,
                terminal=True,
            )
        tracker.add_action("notice", notice, "update")
        return ProgressTrackerEventOutcome(changed=True)

    if isinstance(run_event, Completed):
        runtime_state.final_message = (
            run_event.final_message or runtime_state.final_message
        )
        if runtime_state.final_message.strip():
            tracker.drop_terminal_output_if_duplicate(runtime_state.final_message)
        tracker.clear_transient_action()
        tracker.set_label("done")
        return ProgressTrackerEventOutcome(
            changed=True,
            force=True,
            render_mode="final",
            remove_components=True,
            terminal=True,
        )

    if isinstance(run_event, Failed):
        if tracker.label == "done" and runtime_state.final_message:
            return ProgressTrackerEventOutcome(changed=False)
        runtime_state.error_message = run_event.error_message or "Turn failed"
        tracker.note_error(runtime_state.error_message)
        tracker.clear_transient_action()
        if "interrupt" in runtime_state.error_message.lower():
            tracker.set_label("cancelled")
        else:
            tracker.set_label("failed")
        return ProgressTrackerEventOutcome(
            changed=True,
            force=True,
            remove_components=True,
            terminal=True,
        )

    return ProgressTrackerEventOutcome(changed=False)
