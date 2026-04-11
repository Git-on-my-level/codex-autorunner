"""Post-admission Discord interaction execution.

This module assumes the runtime admission path already normalized the payload,
performed authz checks, and applied any required initial acknowledgement or
defer policy.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.logging_utils import log_event
from .effects import DiscordEffectDeliveryError
from .errors import DiscordTransientError
from .ingress import IngressContext, InteractionKind
from .interaction_registry import (
    dispatch_component_interaction,
    dispatch_modal_submit,
)


async def _respond_empty_autocomplete(service: Any, ctx: IngressContext) -> None:
    try:
        await service.respond_autocomplete(
            ctx.interaction_id,
            ctx.interaction_token,
            choices=[],
        )
    except Exception:  # intentional: fallback response should never escape
        pass


async def handle_component_interaction(
    service: Any,
    ctx: IngressContext,
) -> None:
    custom_id = ctx.custom_id or ""
    try:
        await dispatch_component_interaction(service, ctx)
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service.respond_ephemeral(
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
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "An unexpected error occurred. Please try again later.",
        )


async def handle_modal_submit_interaction(
    service: Any,
    ctx: IngressContext,
) -> None:
    custom_id = ctx.custom_id or ""
    try:
        await dispatch_modal_submit(service, ctx)
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            user_msg,
        )
    except DiscordEffectDeliveryError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.modal.callback_error",
            custom_id=custom_id,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            delivery_status=exc.delivery_status,
            exc=exc,
        )
    except Exception as exc:  # intentional: top-level modal submit error handler
        log_event(
            service._logger,
            logging.ERROR,
            "discord.modal.unhandled_error",
            custom_id=custom_id,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            exc=exc,
        )
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "An unexpected error occurred. Please try again later.",
        )


async def handle_autocomplete_interaction(
    service: Any,
    ctx: IngressContext,
) -> None:
    command_path = ctx.command_spec.path if ctx.command_spec else ()
    options = ctx.command_spec.options if ctx.command_spec else {}
    try:
        await service._handle_command_autocomplete(
            ctx.interaction_id,
            ctx.interaction_token,
            channel_id=ctx.channel_id,
            command_path=command_path,
            options=options,
            focused_name=ctx.focused_name,
            focused_value=ctx.focused_value or "",
        )
    except DiscordTransientError as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.autocomplete.transient_error",
            command_path=command_path,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            user_message=exc.user_message,
            exc=exc,
        )
        await _respond_empty_autocomplete(service, ctx)
    except DiscordEffectDeliveryError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.autocomplete.callback_error",
            command_path=command_path,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            delivery_status=exc.delivery_status,
            exc=exc,
        )
        await _respond_empty_autocomplete(service, ctx)
    except Exception as exc:  # intentional: top-level autocomplete error handler
        log_event(
            service._logger,
            logging.ERROR,
            "discord.autocomplete.unhandled_error",
            command_path=command_path,
            channel_id=ctx.channel_id,
            interaction_id=ctx.interaction_id,
            exc=exc,
        )
        await _respond_empty_autocomplete(service, ctx)


async def execute_ingressed_interaction(
    service: Any,
    ctx: IngressContext,
    payload: dict[str, Any],
) -> None:
    """Route an ingress-acknowledged interaction to its handler.

    The runtime admission path already completed normalization, authz checks,
    and any initial ack/defer work. This is the only execution path for
    interactions submitted to the CommandRunner.
    """
    interaction_id = ctx.interaction_id
    interaction_token = ctx.interaction_token
    channel_id = ctx.channel_id

    if ctx.kind == InteractionKind.AUTOCOMPLETE:
        await handle_autocomplete_interaction(service, ctx)
        return

    if ctx.kind == InteractionKind.COMPONENT:
        if not ctx.custom_id:
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not identify this interaction action. Please retry.",
            )
            return
        await handle_component_interaction(service, ctx)
        return

    if ctx.kind == InteractionKind.MODAL_SUBMIT:
        await handle_modal_submit_interaction(service, ctx)
        return

    command_path = ctx.command_spec.path if ctx.command_spec else ()
    options = ctx.command_spec.options if ctx.command_spec else {}
    if not command_path:
        await service.respond_ephemeral(
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
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Command not implemented yet for Discord.",
            )
    except DiscordTransientError as exc:
        user_msg = exc.user_message or "An error occurred. Please try again later."
        await service.respond_ephemeral(interaction_id, interaction_token, user_msg)
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
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "An unexpected error occurred. Please try again later.",
        )
