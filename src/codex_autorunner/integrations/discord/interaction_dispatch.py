from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...integrations.chat.command_ingress import canonicalize_command_ingress
from .errors import DiscordTransientError
from .ingress import CommandSpec, IngressContext, IngressTiming, InteractionKind

TICKETS_FILTER_SELECT_ID = "tickets_filter_select"
TICKETS_SELECT_ID = "tickets_select"
BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"
AGENT_PROFILE_SELECT_ID = "agent_profile_select"
MODEL_EFFORT_SELECT_ID = "model_effort_select"
SESSION_RESUME_SELECT_ID = "session_resume_select"
UPDATE_TARGET_SELECT_ID = "update_target_select"
UPDATE_CONFIRM_PREFIX = "update_confirm"
UPDATE_CANCEL_PREFIX = "update_cancel"
REVIEW_COMMIT_SELECT_ID = "review_commit_select"
FLOW_ACTION_SELECT_PREFIX = "flow_action_select"


async def handle_normalized_interaction(
    service: Any,
    event: Any,
    context: Any,
) -> None:
    payload_str = event.payload or "{}"
    try:
        payload_data = json.loads(payload_str)
    except json.JSONDecodeError:
        payload_data = {}

    interaction_id = payload_data.get(
        "_discord_interaction_id", event.interaction.interaction_id
    )
    interaction_token = payload_data.get("_discord_token")
    channel_id = context.chat_id

    if not interaction_id or not interaction_token or not channel_id:
        service._logger.warning(
            "handle_normalized_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
            bool(interaction_id),
            bool(interaction_token),
            bool(channel_id),
        )
        return

    policy_result = service._evaluate_interaction_collaboration_policy(
        channel_id=context.chat_id,
        guild_id=context.thread_id,
        user_id=context.user_id,
    )
    if not policy_result.command_allowed:
        service._log_collaboration_policy_result(
            channel_id=context.chat_id,
            guild_id=context.thread_id,
            user_id=context.user_id,
            interaction_id=interaction_id,
            result=policy_result,
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "This Discord command is not authorized for this channel/user/guild.",
        )
        return

    if payload_data.get("type") == "component":
        custom_id = payload_data.get("component_id")
        if not custom_id:
            service._logger.debug(
                "handle_normalized_interaction: missing component_id (interaction_id=%s)",
                interaction_id,
            )
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not identify this interaction action. Please retry.",
            )
            return
        ctx = IngressContext(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=channel_id,
            guild_id=payload_data.get("guild_id"),
            user_id=event.from_user_id,
            kind=InteractionKind.COMPONENT,
            custom_id=custom_id,
            values=payload_data.get("values"),
            message_id=context.message_id,
            timing=IngressTiming(),
        )
        await execute_ingressed_interaction(service, ctx, payload_data)
        return

    if payload_data.get("type") == "modal_submit":
        custom_id_raw = payload_data.get("custom_id")
        modal_values_raw = payload_data.get("values")
        custom_id = custom_id_raw if isinstance(custom_id_raw, str) else ""
        modal_values = modal_values_raw if isinstance(modal_values_raw, dict) else {}
        ctx = IngressContext(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=channel_id,
            guild_id=payload_data.get("guild_id"),
            user_id=event.from_user_id,
            kind=InteractionKind.MODAL_SUBMIT,
            custom_id=custom_id,
            modal_values=modal_values,
            timing=IngressTiming(),
        )
        await execute_ingressed_interaction(service, ctx, payload_data)
        return

    if payload_data.get("type") == "autocomplete":
        command_raw = payload_data.get("command")
        command_path = (
            tuple(part for part in str(command_raw).split(":") if part)
            if isinstance(command_raw, str)
            else ()
        )
        autocomplete_payload = payload_data.get("autocomplete")
        focused_name: Optional[str] = None
        focused_value = ""
        if isinstance(autocomplete_payload, dict):
            focused_name_raw = autocomplete_payload.get("name")
            focused_value_raw = autocomplete_payload.get("value")
            if isinstance(focused_name_raw, str) and focused_name_raw.strip():
                focused_name = focused_name_raw.strip()
            if isinstance(focused_value_raw, str):
                focused_value = focused_value_raw
        options = (
            payload_data.get("options")
            if isinstance(payload_data.get("options"), dict)
            else {}
        )
        ctx = IngressContext(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=channel_id,
            guild_id=payload_data.get("guild_id"),
            user_id=event.from_user_id,
            kind=InteractionKind.AUTOCOMPLETE,
            command_spec=CommandSpec(
                path=command_path,
                options=options,
                ack_policy=None,
                ack_timing="dispatch",
                requires_workspace=False,
            ),
            focused_name=focused_name,
            focused_value=focused_value,
            timing=IngressTiming(),
        )
        await execute_ingressed_interaction(service, ctx, payload_data)
        return

    ingress = canonicalize_command_ingress(
        command=payload_data.get("command"),
        options=payload_data.get("options"),
    )
    command = ingress.command if ingress is not None else ""
    guild_id = payload_data.get("guild_id")

    if ingress is None:
        service._logger.warning(
            "handle_normalized_interaction: failed to canonicalize command ingress (payload=%s)",
            payload_data,
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "I could not parse this interaction. Please retry the command.",
        )
        return

    ingress = replace(
        ingress,
        command_path=service._normalize_discord_command_path(ingress.command_path),
    )
    prepared = await service._prepare_command_interaction_or_abort(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        command_path=ingress.command_path,
        timing="dispatch",
    )
    if not prepared:
        return

    try:
        if ingress.command_path[:1] == ("car",):
            await service._handle_car_command(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=context.thread_id,
                user_id=event.from_user_id,
                command_path=ingress.command_path,
                options=ingress.options,
            )
        elif ingress.command_path[:1] == ("pma",):
            await service._handle_pma_command_from_normalized(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                command_path=ingress.command_path,
                options=ingress.options,
            )
        else:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Command not implemented yet for Discord.",
            )
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service._respond_ephemeral(interaction_id, interaction_token, user_msg)
    except (
        Exception
    ) as exc:  # intentional: top-level normalized interaction error handler
        log_event(
            service._logger,
            logging.ERROR,
            "discord.interaction.unhandled_error",
            command=command,
            channel_id=channel_id,
            exc=exc,
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "An unexpected error occurred. Please try again later.",
        )


async def handle_component_interaction(
    service: Any,
    ctx: IngressContext,
) -> None:
    interaction_id = ctx.interaction_id
    interaction_token = ctx.interaction_token
    channel_id = ctx.channel_id
    custom_id = ctx.custom_id or ""
    values = ctx.values
    guild_id = ctx.guild_id
    user_id = ctx.user_id
    message_id = ctx.message_id
    try:
        if custom_id == TICKETS_FILTER_SELECT_ID:
            await service._handle_ticket_filter_component(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                values=values,
            )
            return

        if custom_id == TICKETS_SELECT_ID:
            await service._handle_ticket_select_component(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                values=values,
            )
            return

        if custom_id.startswith(f"{BIND_PAGE_CUSTOM_ID_PREFIX}:"):
            page_token = custom_id.split(":", 1)[1].strip()
            await service._handle_bind_page_component(
                interaction_id,
                interaction_token,
                page_token=page_token,
            )
            return

        if custom_id == "bind_select":
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a repository and try again.",
                )
                return
            await service._handle_bind_selection(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                selected_workspace_value=values[0],
            )
            return

        if custom_id == "flow_runs_select":
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a run and try again.",
                )
                return
            workspace_root = await service._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root:
                await service._handle_flow_status(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options={"run_id": values[0]},
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
            return

        if custom_id == "agent_select":
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select an agent and try again.",
                )
                return
            await service._handle_car_agent(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options={"name": values[0]},
            )
            return

        if custom_id == AGENT_PROFILE_SELECT_ID:
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a Hermes profile and try again.",
                )
                return
            await service._handle_agent_profile_picker_selection(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                selected_profile=values[0],
            )
            return

        if custom_id == "model_select":
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a model and try again.",
                )
                return
            await service._handle_model_picker_selection(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                user_id=user_id,
                selected_model=values[0],
            )
            return

        if custom_id == MODEL_EFFORT_SELECT_ID:
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select reasoning effort and try again.",
                )
                return
            await service._handle_model_effort_selection(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                user_id=user_id,
                selected_effort=values[0],
            )
            return

        if custom_id == SESSION_RESUME_SELECT_ID:
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a thread and try again.",
                )
                return
            await service._handle_car_resume(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options={"thread_id": values[0]},
            )
            return

        if custom_id == UPDATE_TARGET_SELECT_ID:
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select an update target and try again.",
                )
                return
            await service._handle_car_update(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options={"target": values[0]},
                response_mode="component",
            )
            return

        if custom_id.startswith(f"{UPDATE_CONFIRM_PREFIX}:"):
            raw_target = custom_id.split(":", 1)[1].strip()
            if not raw_target:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select an update target and try again.",
                )
                return
            await service._handle_car_update(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options={"target": raw_target, "confirmed": True},
                response_mode="component",
            )
            return

        if custom_id.startswith(f"{UPDATE_CANCEL_PREFIX}:"):
            await service._update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text="Update cancelled.",
                components=[],
            )
            return

        if custom_id == REVIEW_COMMIT_SELECT_ID:
            if not values:
                await service._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Please select a commit and try again.",
                )
                return
            workspace_root = await service._require_bound_workspace(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            if workspace_root:
                await service._handle_car_review(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    workspace_root=workspace_root,
                    options={"target": f"commit {values[0]}"},
                )
            return

        if custom_id.startswith(f"{FLOW_ACTION_SELECT_PREFIX}:"):
            await _handle_flow_action_select(service, ctx)
            return

        if custom_id.startswith("flow:"):
            workspace_root = await service._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root:
                await service._handle_flow_button(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    custom_id=custom_id,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
            return

        if custom_id.startswith("approval:"):
            await service._handle_approval_component(
                interaction_id,
                interaction_token,
                custom_id=custom_id,
            )
            return

        if custom_id.startswith("queue_cancel:"):
            await service._handle_queue_cancel_button(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                custom_id=custom_id,
                message_id=message_id,
                guild_id=guild_id,
            )
            return

        if custom_id.startswith("queue_interrupt_send:"):
            await service._handle_queue_interrupt_send_button(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                custom_id=custom_id,
                message_id=message_id,
                guild_id=guild_id,
                user_id=user_id,
            )
            return

        if custom_id == "cancel_turn" or custom_id.startswith("cancel_turn:"):
            await service._handle_cancel_turn_button(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                user_id=user_id,
                message_id=message_id,
                custom_id=custom_id,
            )
            return

        if custom_id == "continue_turn":
            await service._handle_continue_turn_button(
                interaction_id,
                interaction_token,
            )
            return

        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown component: {custom_id}",
        )
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service._respond_ephemeral(interaction_id, interaction_token, user_msg)
    except (
        Exception
    ) as exc:  # intentional: top-level component interaction error handler
        log_event(
            service._logger,
            logging.ERROR,
            "discord.component.normalized.unhandled_error",
            custom_id=custom_id,
            channel_id=channel_id,
            exc=exc,
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "An unexpected error occurred. Please try again later.",
        )


async def _handle_flow_action_select(
    service: Any,
    ctx: IngressContext,
) -> None:
    from ...core.flows import FLOW_ACTIONS_WITH_RUN_PICKER

    interaction_id = ctx.interaction_id
    interaction_token = ctx.interaction_token
    channel_id = ctx.channel_id
    custom_id = ctx.custom_id or ""
    values = ctx.values
    guild_id = ctx.guild_id
    user_id = ctx.user_id

    action = custom_id.split(":", 1)[1].strip().lower()
    if not values:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Please select a run and try again.",
        )
        return
    if action not in FLOW_ACTIONS_WITH_RUN_PICKER:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown flow action picker: {action}",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if not workspace_root:
        return
    run_id = values[0]
    if action == "status":
        await service._handle_flow_status(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if action == "restart":
        await service._handle_flow_restart(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
        )
        return
    if action == "resume":
        await service._handle_flow_resume(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if action == "stop":
        await service._handle_flow_stop(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if action == "archive":
        await service._handle_flow_archive(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if action == "recover":
        await service._handle_flow_recover(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
        )
        return
    if action == "reply":
        pending_key = service._pending_interaction_scope_key(
            channel_id=channel_id,
            user_id=user_id,
        )
        pending_text = service._pending_flow_reply_text.pop(pending_key, None)
        if not isinstance(pending_text, str) or not pending_text.strip():
            deferred = await service._defer_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
            await service._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Reply selection expired. Re-run `/flow reply text:<...>`.",
            )
            return
        await service._handle_flow_reply(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id, "text": pending_text},
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )
        return


async def execute_ingressed_interaction(
    service: Any,
    ctx: IngressContext,
    payload: dict[str, Any],
) -> None:
    """Route an ingress-acknowledged interaction to its handler.

    Unlike handle_normalized_interaction, this entry point skips authz checks
    and ack/defer because ingress already completed those steps.  It is the
    required execution path for interactions submitted to the CommandRunner.
    """
    interaction_id = ctx.interaction_id
    interaction_token = ctx.interaction_token
    channel_id = ctx.channel_id

    if ctx.kind == InteractionKind.AUTOCOMPLETE:
        command_path = ctx.command_spec.path if ctx.command_spec else ()
        options = ctx.command_spec.options if ctx.command_spec else {}
        await service._handle_command_autocomplete(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            command_path=command_path,
            options=options,
            focused_name=ctx.focused_name,
            focused_value=ctx.focused_value or "",
        )
        return

    if ctx.kind == InteractionKind.COMPONENT:
        if not ctx.custom_id:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not identify this interaction action. Please retry.",
            )
            return
        await handle_component_interaction(service, ctx)
        return

    if ctx.kind == InteractionKind.MODAL_SUBMIT:
        await service._handle_ticket_modal_submit(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=ctx.custom_id or "",
            values=ctx.modal_values or {},
        )
        return

    command_path = ctx.command_spec.path if ctx.command_spec else ()
    options = ctx.command_spec.options if ctx.command_spec else {}
    if not command_path:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "I could not parse this interaction. Please retry the command.",
        )
        return

    try:
        if command_path[:1] == ("car",):
            await service._handle_car_command(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=ctx.guild_id,
                user_id=ctx.user_id,
                command_path=command_path,
                options=options,
            )
        elif command_path[:1] == ("pma",):
            await service._handle_pma_command(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=ctx.guild_id,
                command_path=command_path,
                options=options,
            )
        else:
            await service._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Command not implemented yet for Discord.",
            )
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service._respond_ephemeral(interaction_id, interaction_token, user_msg)
    except Exception as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.runner.handler_error",
            command_path=command_path,
            channel_id=channel_id,
            interaction_id=interaction_id,
            exc=exc,
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "An unexpected error occurred. Please try again later.",
        )
