from __future__ import annotations

import contextlib
import logging
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....integrations.chat.chat_ux_telemetry import (
    ChatUxMilestone,
    ChatUxTimingSnapshot,
)
from ..errors import DiscordAPIError
from ..interaction_runtime import (
    defer_and_update_runtime_component_message,
    ensure_ephemeral_response_deferred,
)

_logger = logging.getLogger(__name__)


async def send_interrupt_component_response(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    text: str,
) -> None:
    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def handle_cancel_turn_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    user_id: Optional[str] = None,
    message_id: Optional[str] = None,
    custom_id: str = "cancel_turn",
) -> None:
    from ..components import parse_cancel_turn_custom_id

    thread_target_id, execution_id = parse_cancel_turn_custom_id(custom_id)
    await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        "Stopping current turn...",
        components=[],
    )

    cancel_snapshot = ChatUxTimingSnapshot(platform="discord", channel_id=channel_id)
    cancel_snapshot.record(ChatUxMilestone.RAW_EVENT_RECEIVED)
    cancel_snapshot.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE)
    log_event(
        getattr(service, "_logger", _logger),
        logging.INFO,
        "discord.turn.cancel_acknowledged",
        channel_id=channel_id,
        thread_target_id=thread_target_id,
        execution_id=execution_id,
        **cancel_snapshot.to_log_fields(),
    )
    await service._handle_car_interrupt(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        source="component",
        thread_target_id=thread_target_id,
        execution_id=execution_id,
        source_custom_id=custom_id,
        source_message_id=message_id,
        source_user_id=user_id,
    )


async def handle_cancel_queued_turn_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    message_id: Optional[str] = None,
) -> None:
    from ..components import parse_cancel_queued_turn_custom_id
    from ..message_turns import clear_discord_turn_progress_leases

    execution_id = parse_cancel_queued_turn_custom_id(custom_id)
    if not execution_id:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    binding = await service._store.get_binding(channel_id=channel_id)
    pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
    mode = "pma" if pma_enabled else "repo"
    orchestration_service, _binding_row, current_thread = (
        service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
    )
    if current_thread is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    cancelled = orchestration_service.cancel_queued_execution(
        current_thread.thread_target_id,
        execution_id,
    )
    if not cancelled:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request is no longer pending.",
        )
        return
    await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        "Cancelling queued request...",
        components=[],
    )
    await clear_discord_turn_progress_leases(
        service,
        managed_thread_id=current_thread.thread_target_id,
        execution_id=execution_id,
    )
    if message_id:
        with contextlib.suppress(
            DiscordAPIError,
            RuntimeError,
            ConnectionError,
            OSError,
            ValueError,
        ):
            await service._rest.edit_channel_message(
                channel_id=channel_id,
                message_id=message_id,
                payload={
                    "content": "Queued request cancelled.",
                    "components": [],
                },
            )
    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        "Queued request cancelled.",
    )


async def handle_queued_turn_interrupt_send_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    user_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    from ..components import parse_queued_turn_interrupt_send_custom_id

    execution_id, source_message_id = parse_queued_turn_interrupt_send_custom_id(
        custom_id
    )
    if not execution_id or not source_message_id:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    binding = await service._store.get_binding(channel_id=channel_id)
    pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
    mode = "pma" if pma_enabled else "repo"
    orchestration_service, _binding_row, current_thread = (
        service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
    )
    if current_thread is None:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    promoted = orchestration_service.promote_queued_execution(
        current_thread.thread_target_id,
        execution_id,
    )
    if not promoted:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request is no longer pending.",
        )
        return
    get_running_execution = getattr(
        orchestration_service,
        "get_running_execution",
        None,
    )
    if callable(get_running_execution):
        running_execution = get_running_execution(current_thread.thread_target_id)
        if running_execution is None:
            await send_interrupt_component_response(
                service,
                interaction_id,
                interaction_token,
                "Queued request moved to the front.",
            )
            return
    await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        "Message received. Switching to it now...",
        components=[],
    )
    await service._handle_car_interrupt(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        active_turn_text="Message received. Switching to it now...",
        cancel_queued=False,
        allow_promoted_no_active_success=True,
        progress_reuse_source_message_id=source_message_id,
        progress_reuse_acknowledgement="Message received. Switching to it now...",
        source="component",
        source_custom_id=custom_id,
        source_message_id=message_id,
        source_user_id=user_id,
    )


async def handle_queue_cancel_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    guild_id: Optional[str],
    message_id: Optional[str] = None,
) -> None:
    source_message_id = custom_id.split(":", 1)[1].strip()
    if not source_message_id:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    conversation_id = service._dispatcher_conversation_id(
        channel_id=channel_id,
        guild_id=guild_id,
    )
    cancelled = await service._dispatcher.cancel_pending_message(
        conversation_id,
        source_message_id,
    )
    if not cancelled:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request is no longer pending.",
        )
        return
    await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        "Cancelling queued request...",
        components=[],
    )
    await service._clear_queued_notice(
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        channel_id=channel_id,
    )
    if message_id:
        service._queued_notice_messages.pop((conversation_id, source_message_id), None)
    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        "Queued request cancelled.",
    )


async def handle_queue_interrupt_send_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    guild_id: Optional[str],
    user_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    source_message_id = custom_id.split(":", 1)[1].strip()
    if not source_message_id:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request is unavailable.",
        )
        return
    conversation_id = service._dispatcher_conversation_id(
        channel_id=channel_id,
        guild_id=guild_id,
    )
    promoted = await service._dispatcher.promote_pending_message(
        conversation_id,
        source_message_id,
    )
    if not promoted:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request is no longer pending.",
        )
        return
    binding = await service._store.get_binding(channel_id=channel_id)
    pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
    mode = "pma" if pma_enabled else "repo"
    _orchestration_service, _binding_row, current_thread = (
        service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
    )
    if current_thread is None:
        await send_interrupt_component_response(
            service,
            interaction_id,
            interaction_token,
            "Queued request moved to the front.",
        )
        return
    await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        "Message received. Switching to it now...",
        components=[],
    )
    await service._handle_car_interrupt(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        active_turn_text="Message received. Switching to it now...",
        progress_reuse_source_message_id=source_message_id,
        progress_reuse_acknowledgement="Message received. Switching to it now...",
        source="component",
        source_custom_id=custom_id,
        source_message_id=message_id,
        source_user_id=user_id,
    )


async def handle_continue_turn_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
) -> None:
    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        (
            "Compaction complete. Send your next message to continue this "
            "session, or use `/car new` to start a fresh session."
        ),
    )
