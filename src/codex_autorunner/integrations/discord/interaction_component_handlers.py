"""Component interaction handler shims for the Discord interaction registry.

Extracted from interaction_registry.py to reduce mixed ownership in the registry
module. Each handler shim extracts context from the IngressContext and delegates
to the appropriate service method.
"""

from __future__ import annotations

from typing import Any, Optional

from ...core.flows.catalog import FLOW_ACTIONS_WITH_RUN_PICKER
from .interaction_runtime import (
    defer_and_update_runtime_component_message,
    ensure_ephemeral_response_deferred,
)

BIND_SELECT_CUSTOM_ID = "bind_select"
BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"
FLOW_RUNS_SELECT_ID = "flow_runs_select"
AGENT_SELECT_CUSTOM_ID = "agent_select"
AGENT_PROFILE_SELECT_ID = "agent_profile_select"
MODEL_SELECT_CUSTOM_ID = "model_select"
MODEL_EFFORT_SELECT_ID = "model_effort_select"
SESSION_RESUME_SELECT_ID = "session_resume_select"
UPDATE_TARGET_SELECT_ID = "update_target_select"
UPDATE_CONFIRM_PREFIX = "update_confirm"
UPDATE_CANCEL_PREFIX = "update_cancel"
REVIEW_COMMIT_SELECT_ID = "review_commit_select"
FLOW_ACTION_SELECT_PREFIX = "flow_action_select"
TICKETS_FILTER_SELECT_ID, TICKETS_SELECT_ID = "tickets_filter_select", "tickets_select"
TICKETS_PAGE_CUSTOM_ID_PREFIX, TICKETS_BACK_CUSTOM_ID = "tickets_page", "tickets_back"
TICKETS_CHUNK_CUSTOM_ID_PREFIX, TICKETS_MODAL_PREFIX = "tickets_chunk", "tickets_modal"
CONTEXTSPACE_SELECT_ID, CONTEXTSPACE_BACK_CUSTOM_ID = (
    "contextspace_select",
    "contextspace_back",
)
CONTEXTSPACE_PAGE_CUSTOM_ID_PREFIX = "contextspace_page"
CONTEXTSPACE_CHUNK_CUSTOM_ID_PREFIX = "contextspace_chunk"
NEWT_HARD_RESET_CUSTOM_ID = "newt_hard_reset"
NEWT_CANCEL_CUSTOM_ID = "newt_cancel"


async def _handle_bind_page_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    page_token = custom_id.split(":", 1)[1].strip()
    await service._handle_bind_page_component(
        ctx.interaction_id,
        ctx.interaction_token,
        page_token=page_token,
    )


async def _handle_bind_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a repository and try again.",
        )
        return
    await service._handle_bind_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
        selected_workspace_value=ctx.values[0],
    )


async def _handle_flow_runs_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a run and try again.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_flow_status(
        ctx.interaction_id,
        ctx.interaction_token,
        workspace_root=workspace_root,
        options={"run_id": ctx.values[0]},
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
    )


async def _handle_agent_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an agent and try again.",
        )
        return
    await service._handle_car_agent(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"name": ctx.values[0]},
    )


async def _handle_agent_profile_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a Hermes profile and try again.",
        )
        return
    await service._handle_agent_profile_picker_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        selected_profile=ctx.values[0],
    )


async def _handle_model_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a model and try again.",
        )
        return
    await service._handle_model_picker_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        selected_model=ctx.values[0],
    )


async def _handle_model_effort_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select reasoning effort and try again.",
        )
        return
    await service._handle_model_effort_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        selected_effort=ctx.values[0],
    )


async def _handle_session_resume_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a thread and try again.",
        )
        return
    await service._handle_car_resume(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"thread_id": ctx.values[0]},
    )


async def _handle_update_target_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an update target and try again.",
        )
        return
    await service._handle_car_update(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"target": ctx.values[0]},
        response_mode="component",
    )


async def _handle_update_confirm_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    raw_target = custom_id.split(":", 1)[1].strip()
    if not raw_target:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an update target and try again.",
        )
        return
    await service._handle_car_update(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"target": raw_target, "confirmed": True},
        response_mode="component",
    )


async def _handle_update_cancel_component(service: Any, ctx: Any) -> None:
    await service.update_component_message(
        interaction_id=ctx.interaction_id,
        interaction_token=ctx.interaction_token,
        text="Update cancelled.",
        components=[],
    )


def _parse_newt_component_custom_id(custom_id: str, prefix: str) -> Optional[str]:
    if custom_id == prefix:
        return ""
    if not custom_id.startswith(f"{prefix}:"):
        return None
    token = custom_id.split(":", 1)[1].strip()
    return token or None


async def _handle_newt_hard_reset_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    hard_reset_token = _parse_newt_component_custom_id(
        custom_id, NEWT_HARD_RESET_CUSTOM_ID
    )
    if hard_reset_token is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "I could not identify this interaction action. Please retry.",
        )
        return
    await service._handle_car_newt_hard_reset(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        expected_workspace_token=hard_reset_token,
    )


async def _handle_newt_cancel_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    cancel_token = _parse_newt_component_custom_id(custom_id, NEWT_CANCEL_CUSTOM_ID)
    if cancel_token is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "I could not identify this interaction action. Please retry.",
        )
        return
    await service._handle_car_newt_cancel(
        ctx.interaction_id,
        ctx.interaction_token,
        expected_workspace_token=cancel_token,
    )


async def _handle_review_commit_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a commit and try again.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_car_review(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        workspace_root=workspace_root,
        options={"target": f"commit {ctx.values[0]}"},
    )


async def _handle_flow_action_select_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    action = custom_id.split(":", 1)[1].strip().lower()
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a run and try again.",
        )
        return
    if action not in FLOW_ACTIONS_WITH_RUN_PICKER:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            f"Unknown flow action picker: {action}",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    run_id = ctx.values[0]
    handler_name = {
        "status": "_handle_flow_status",
        "restart": "_handle_flow_restart",
        "resume": "_handle_flow_resume",
        "stop": "_handle_flow_stop",
        "archive": "_handle_flow_archive",
        "recover": "_handle_flow_recover",
        "reply": "_handle_flow_reply",
    }.get(action)
    if handler_name is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            f"Unknown flow action picker: {action}",
        )
        return
    kwargs: dict[str, Any] = {
        "workspace_root": workspace_root,
        "options": {"run_id": run_id},
    }
    if action in {"status", "resume", "stop", "archive", "reply"}:
        kwargs["channel_id"] = ctx.channel_id
        kwargs["guild_id"] = ctx.guild_id
    if action == "reply":
        pending_key = service._pending_interaction_scope_key(
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
        )
        pending_text = service._pending_flow_reply_text.pop(pending_key, None)
        if not isinstance(pending_text, str) or not pending_text.strip():
            deferred = await ensure_ephemeral_response_deferred(
                service,
                ctx.interaction_id,
                ctx.interaction_token,
            )
            await service.send_or_respond_ephemeral(
                interaction_id=ctx.interaction_id,
                interaction_token=ctx.interaction_token,
                deferred=deferred,
                text="Reply selection expired. Re-run `/flow reply text:<...>`.",
            )
            return
        await defer_and_update_runtime_component_message(
            service,
            ctx.interaction_id,
            ctx.interaction_token,
            f"Saving reply and resuming run {run_id}...",
            components=[],
        )
        kwargs["options"] = {"run_id": run_id, "text": pending_text}
        kwargs["user_id"] = ctx.user_id
        kwargs["component_response"] = True
    await getattr(service, handler_name)(
        ctx.interaction_id,
        ctx.interaction_token,
        **kwargs,
    )


async def _handle_flow_button_component(service: Any, ctx: Any) -> None:
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_flow_button(
        ctx.interaction_id,
        ctx.interaction_token,
        workspace_root=workspace_root,
        custom_id=ctx.custom_id or "",
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
    )


async def _handle_approval_component(service: Any, ctx: Any) -> None:
    await service._handle_approval_component(
        ctx.interaction_id,
        ctx.interaction_token,
        custom_id=ctx.custom_id or "",
    )


async def _handle_queue_cancel_component(service: Any, ctx: Any) -> None:
    await service._handle_queue_cancel_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        message_id=ctx.message_id,
        guild_id=ctx.guild_id,
    )


async def _handle_cancel_queued_turn_component(service: Any, ctx: Any) -> None:
    await service._handle_cancel_queued_turn_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        message_id=ctx.message_id,
    )


async def _handle_queue_interrupt_send_component(service: Any, ctx: Any) -> None:
    await service._handle_queue_interrupt_send_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        message_id=ctx.message_id,
        guild_id=ctx.guild_id,
        user_id=ctx.user_id,
    )


async def _handle_queued_turn_interrupt_send_component(service: Any, ctx: Any) -> None:
    await service._handle_queued_turn_interrupt_send_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        user_id=ctx.user_id,
        message_id=ctx.message_id,
    )


async def _handle_cancel_turn_component(service: Any, ctx: Any) -> None:
    await service._handle_cancel_turn_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        message_id=ctx.message_id,
        custom_id=ctx.custom_id or "",
    )


async def _handle_continue_turn_component(service: Any, ctx: Any) -> None:
    await service._handle_continue_turn_button(
        ctx.interaction_id,
        ctx.interaction_token,
    )
