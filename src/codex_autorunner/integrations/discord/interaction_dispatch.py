from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...integrations.chat.command_ingress import canonicalize_command_ingress
from .effects import DiscordEffectDeliveryError
from .errors import DiscordTransientError
from .ingress import CommandSpec, IngressContext, IngressTiming, InteractionKind
from .interaction_registry import (
    dispatch_component_interaction,
    dispatch_modal_submit,
)


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
        service._ensure_interaction_session(
            interaction_id,
            interaction_token,
            kind=InteractionKind.COMPONENT,
        )
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
        service._ensure_interaction_session(
            interaction_id,
            interaction_token,
            kind=InteractionKind.MODAL_SUBMIT,
        )
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
        service._ensure_interaction_session(
            interaction_id,
            interaction_token,
            kind=InteractionKind.AUTOCOMPLETE,
        )
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
    service._ensure_interaction_session(
        interaction_id,
        interaction_token,
        kind=InteractionKind.SLASH_COMMAND,
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
                guild_id=guild_id,
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
    except DiscordEffectDeliveryError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.interaction.callback_error",
            command=command,
            channel_id=channel_id,
            interaction_id=interaction_id,
            delivery_status=exc.delivery_status,
            exc=exc,
        )
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
    custom_id = ctx.custom_id or ""
    try:
        await dispatch_component_interaction(service, ctx)
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service._respond_ephemeral(
            ctx.interaction_id, ctx.interaction_token, user_msg
        )
    except DiscordEffectDeliveryError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.component.callback_error",
            custom_id=custom_id,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            delivery_status=exc.delivery_status,
            exc=exc,
        )
    except (
        Exception
    ) as exc:  # intentional: top-level component interaction error handler
        log_event(
            service._logger,
            logging.ERROR,
            "discord.component.normalized.unhandled_error",
            custom_id=custom_id,
            channel_id=ctx.channel_id,
            exc=exc,
        )
        await service._respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "An unexpected error occurred. Please try again later.",
        )


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
        await dispatch_modal_submit(service, ctx)
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
    except DiscordEffectDeliveryError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.runner.callback_error",
            command_path=command_path,
            channel_id=channel_id,
            interaction_id=interaction_id,
            delivery_status=exc.delivery_status,
            exc=exc,
        )
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
