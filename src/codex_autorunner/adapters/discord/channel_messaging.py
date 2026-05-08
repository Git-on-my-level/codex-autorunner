from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional, cast

from ...core.logging_utils import log_event
from .errors import DiscordAPIError
from .rendering import sanitize_discord_outbound_text
from .state import OutboxRecord


async def send_channel_message(
    rest: Any,
    logger: logging.Logger,
    channel_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(payload)
    content = payload.get("content")
    if isinstance(content, str):
        payload["content"] = sanitize_discord_outbound_text(content)
    content_len = len(payload.get("content", "") or "")
    log_event(
        logger,
        logging.DEBUG,
        "discord.channel_message.sending",
        channel_id=channel_id,
        content_len=content_len,
    )
    response = await rest.create_channel_message(channel_id=channel_id, payload=payload)
    message_id = response.get("id") if isinstance(response, dict) else None
    log_event(
        logger,
        logging.DEBUG,
        "discord.channel_message.sent",
        channel_id=channel_id,
        content_len=content_len,
        message_id=message_id,
    )
    return cast(dict[str, Any], response)


async def delete_channel_message(
    rest: Any,
    channel_id: str,
    message_id: str,
) -> None:
    await rest.delete_channel_message(
        channel_id=channel_id,
        message_id=message_id,
    )


async def send_channel_message_safe(
    store: Any,
    rest: Any,
    logger: logging.Logger,
    send_fn: Any,
    channel_id: str,
    payload: dict[str, Any],
    *,
    record_id: Optional[str] = None,
) -> bool:
    try:
        await send_fn(channel_id, payload)
        return True
    except (DiscordAPIError, OSError, RuntimeError) as exc:
        outbox_record_id = record_id or f"retry:{channel_id}:{uuid.uuid4().hex[:12]}"
        log_event(
            logger,
            logging.WARNING,
            "discord.channel_message.send_failed",
            channel_id=channel_id,
            record_id=outbox_record_id,
            exc=exc,
        )
        try:
            await store.enqueue_outbox(
                OutboxRecord(
                    record_id=outbox_record_id,
                    channel_id=channel_id,
                    message_id=None,
                    operation="send",
                    payload_json=dict(payload),
                )
            )
        except (OSError, ValueError, TypeError) as enqueue_exc:
            log_event(
                logger,
                logging.ERROR,
                "discord.channel_message.enqueue_failed",
                channel_id=channel_id,
                record_id=outbox_record_id,
                exc=enqueue_exc,
            )
    return False


async def delete_channel_message_safe(
    store: Any,
    delete_fn: Any,
    logger: logging.Logger,
    channel_id: str,
    message_id: str,
    *,
    record_id: Optional[str] = None,
) -> bool:
    if not isinstance(message_id, str) or not message_id:
        return False
    try:
        await delete_fn(channel_id, message_id)
        return True
    except (DiscordAPIError, OSError, RuntimeError) as exc:
        outbox_record_id = (
            record_id or f"retry:delete:{channel_id}:{uuid.uuid4().hex[:12]}"
        )
        log_event(
            logger,
            logging.WARNING,
            "discord.channel_message.delete_failed",
            channel_id=channel_id,
            message_id=message_id,
            record_id=outbox_record_id,
            exc=exc,
        )
        try:
            await store.enqueue_outbox(
                OutboxRecord(
                    record_id=outbox_record_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    operation="delete",
                    payload_json={},
                )
            )
        except (OSError, ValueError) as enqueue_exc:
            log_event(
                logger,
                logging.ERROR,
                "discord.channel_message.delete_enqueue_failed",
                channel_id=channel_id,
                message_id=message_id,
                record_id=outbox_record_id,
                exc=enqueue_exc,
            )
    return False


async def handle_discord_outbox_delivery(
    hub_client: Any,
    logger: logging.Logger,
    record: OutboxRecord,
    delivered_message_id: Optional[str],
) -> None:
    if not isinstance(delivered_message_id, str) or not delivered_message_id:
        return
    if hub_client is None:
        log_event(
            logger,
            logging.WARNING,
            "discord.outbox.delivery_mark.hub_client_unavailable",
            record_id=record.record_id,
        )
        return
    from ...core.hub_control_plane import (
        NotificationDeliveryMarkRequest,
    )

    try:
        await hub_client.mark_notification_delivered(
            NotificationDeliveryMarkRequest(
                delivery_record_id=record.record_id,
                delivered_message_id=delivered_message_id,
            )
        )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "discord.outbox.delivery_mark.control_plane_failed",
            record_id=record.record_id,
            exc=exc,
        )


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _first_non_empty_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _nested_text(payload: dict[str, Any], key: str, field: str) -> Optional[str]:
    candidate = payload.get(key)
    if not isinstance(candidate, dict):
        return None
    return _first_non_empty_text(candidate.get(field))


async def resolve_channel_name(service: Any, channel_id: str) -> Optional[str]:
    fetch = getattr(service._rest, "get_channel", None)
    if not callable(fetch):
        service._channel_name_cache[channel_id] = ""
        return None
    in_flight = service._channel_name_lookups.get(channel_id)
    if in_flight is None:

        async def _load_channel_name() -> Optional[str]:
            try:
                payload = await fetch(channel_id=channel_id)
            except (DiscordAPIError, OSError) as exc:
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.channel_directory.channel_lookup_failed",
                    channel_id=channel_id,
                    exc=exc,
                )
                service._channel_name_cache[channel_id] = ""
                return None
            if not isinstance(payload, dict):
                service._channel_name_cache[channel_id] = ""
                return None
            channel_label = _first_non_empty_text(payload.get("name"))
            if channel_label is None:
                service._channel_name_cache[channel_id] = ""
                return None
            normalized = channel_label.lstrip("#")
            service._channel_name_cache[channel_id] = normalized
            return normalized

        in_flight = asyncio.create_task(_load_channel_name())
        service._channel_name_lookups[channel_id] = in_flight
    try:
        return cast(Optional[str], await in_flight)
    finally:
        if service._channel_name_lookups.get(channel_id) is in_flight:
            service._channel_name_lookups.pop(channel_id, None)


async def resolve_guild_name(service: Any, guild_id: str) -> Optional[str]:
    fetch = getattr(service._rest, "get_guild", None)
    if not callable(fetch):
        service._guild_name_cache[guild_id] = ""
        return None
    in_flight = service._guild_name_lookups.get(guild_id)
    if in_flight is None:

        async def _load_guild_name() -> Optional[str]:
            try:
                payload = await fetch(guild_id=guild_id)
            except (DiscordAPIError, OSError) as exc:
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.channel_directory.guild_lookup_failed",
                    guild_id=guild_id,
                    exc=exc,
                )
                service._guild_name_cache[guild_id] = ""
                return None
            if not isinstance(payload, dict):
                service._guild_name_cache[guild_id] = ""
                return None
            guild_label = _first_non_empty_text(payload.get("name"))
            if guild_label is None:
                service._guild_name_cache[guild_id] = ""
                return None
            service._guild_name_cache[guild_id] = guild_label
            return guild_label

        in_flight = asyncio.create_task(_load_guild_name())
        service._guild_name_lookups[guild_id] = in_flight
    try:
        return cast(Optional[str], await in_flight)
    finally:
        if service._guild_name_lookups.get(guild_id) is in_flight:
            service._guild_name_lookups.pop(guild_id, None)


async def record_channel_directory_seen(service: Any, payload: dict[str, Any]) -> None:
    channel_id = _coerce_id(payload.get("channel_id"))
    if channel_id is None:
        return
    guild_id = _coerce_id(payload.get("guild_id"))

    guild_label = _first_non_empty_text(
        payload.get("guild_name"),
        _nested_text(payload, "guild", "name"),
    )
    channel_label_raw = _first_non_empty_text(
        payload.get("channel_name"),
        _nested_text(payload, "channel", "name"),
    )
    if channel_label_raw is not None:
        channel_label_raw = channel_label_raw.lstrip("#")
        service._channel_name_cache[channel_id] = channel_label_raw
    else:
        if channel_id in service._channel_name_cache:
            cached_channel = service._channel_name_cache[channel_id]
            channel_label_raw = cached_channel if cached_channel else None
        else:
            channel_label_raw = await resolve_channel_name(service, channel_id)

    if guild_id is not None:
        if guild_label is not None:
            service._guild_name_cache[guild_id] = guild_label
        else:
            if guild_id in service._guild_name_cache:
                cached_guild = service._guild_name_cache[guild_id]
                guild_label = cached_guild if cached_guild else None
            else:
                guild_label = await resolve_guild_name(service, guild_id)

    channel_label = (
        f"#{channel_label_raw.lstrip('#')}"
        if channel_label_raw is not None
        else f"#{channel_id}"
    )

    if guild_id is not None:
        display = f"{guild_label or f'guild:{guild_id}'} / {channel_label}"
    else:
        display = channel_label if channel_label_raw is not None else channel_id

    meta: dict[str, Any] = {}
    if guild_id is not None:
        meta["guild_id"] = guild_id

    try:
        service._channel_directory_store.record_seen(
            "discord",
            channel_id,
            None,
            display,
            meta,
        )
    except (OSError, ValueError, TypeError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.channel_directory.record_failed",
            channel_id=channel_id,
            guild_id=guild_id,
            exc=exc,
        )
