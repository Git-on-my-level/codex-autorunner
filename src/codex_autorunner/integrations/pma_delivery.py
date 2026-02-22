from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..core.logging_utils import log_event
from ..core.pma_sink import PmaActiveSinkStore
from ..core.time_utils import now_iso
from ..integrations.chat.text_chunking import chunk_text
from ..integrations.discord.constants import DISCORD_MAX_MESSAGE_LENGTH
from ..integrations.discord.state import (
    DiscordStateStore,
)
from ..integrations.discord.state import (
    OutboxRecord as DiscordOutboxRecord,
)
from ..integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ..integrations.telegram.state import OutboxRecord, TelegramStateStore

logger = logging.getLogger(__name__)


def _parse_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw and raw.lstrip("-").isdigit():
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def _resolve_telegram_target(
    sink: dict[str, Any],
) -> Optional[tuple[int, Optional[int]]]:
    kind = sink.get("kind")
    if kind == "telegram":
        chat_id = sink.get("chat_id")
        thread_id = sink.get("thread_id")
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        return chat_id, thread_id
    if kind == "chat" and sink.get("platform") == "telegram":
        chat_id = _parse_int(sink.get("chat_id"))
        if chat_id is None:
            return None
        thread_id = _parse_int(sink.get("thread_id"))
        return chat_id, thread_id
    return None


def _resolve_discord_target(
    sink: dict[str, Any],
) -> Optional[str]:
    kind = sink.get("kind")
    platform = sink.get("platform")
    if kind == "chat" and platform == "discord":
        channel_id = sink.get("chat_id")
        if isinstance(channel_id, str) and channel_id.strip():
            return channel_id.strip()
    return None


async def deliver_pma_output_to_active_sink(
    *,
    hub_root: Path,
    assistant_text: str,
    turn_id: str,
    lifecycle_event: Optional[dict[str, Any]],
    telegram_state_path: Path,
    discord_state_path: Optional[Path] = None,
) -> bool:
    if not lifecycle_event:
        return False
    if not assistant_text or not assistant_text.strip():
        return False
    if not isinstance(turn_id, str) or not turn_id:
        return False

    sink_store = PmaActiveSinkStore(hub_root)
    sink = sink_store.load()
    if not isinstance(sink, dict):
        return False

    last_delivery = sink.get("last_delivery_turn_id")
    if isinstance(last_delivery, str) and last_delivery == turn_id:
        return False

    discord_channel_id = _resolve_discord_target(sink)
    if discord_channel_id:
        return await _deliver_to_discord(
            hub_root=hub_root,
            channel_id=discord_channel_id,
            assistant_text=assistant_text,
            turn_id=turn_id,
            discord_state_path=discord_state_path,
        )

    target = _resolve_telegram_target(sink)
    if target is None:
        return False

    chat_id, thread_id = target

    chunks = chunk_text(
        assistant_text, max_len=TELEGRAM_MAX_MESSAGE_LENGTH, with_numbering=True
    )
    if not chunks:
        return False

    store = TelegramStateStore(telegram_state_path)
    try:
        for idx, chunk in enumerate(chunks, 1):
            record_id = f"pma:{turn_id}:{idx}"
            record = OutboxRecord(
                record_id=record_id,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=None,
                placeholder_message_id=None,
                text=chunk,
                created_at=now_iso(),
                operation="send",
                outbox_key=record_id,
            )
            await store.enqueue_outbox(record)
    except Exception:
        logger.exception("Failed to enqueue PMA output to Telegram outbox")
        return False
    finally:
        await store.close()

    sink_store.mark_delivered(turn_id)
    return True


async def _deliver_to_discord(
    *,
    hub_root: Path,
    channel_id: str,
    assistant_text: str,
    turn_id: str,
    discord_state_path: Optional[Path] = None,
) -> bool:
    if discord_state_path is None:
        discord_state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    chunks = chunk_text(
        assistant_text, max_len=DISCORD_MAX_MESSAGE_LENGTH, with_numbering=True
    )
    if not chunks:
        return False

    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        for idx, chunk in enumerate(chunks, 1):
            record_id = f"pma:{turn_id}:{idx}"
            record = DiscordOutboxRecord(
                record_id=record_id,
                channel_id=channel_id,
                message_id=None,
                operation="send",
                payload_json={"content": chunk},
                created_at=now_iso(),
            )
            await store.enqueue_outbox(record)
    except Exception:
        logger.exception("Failed to enqueue PMA output to Discord outbox")
        return False
    finally:
        await store.close()

    sink_store = PmaActiveSinkStore(hub_root)
    sink_store.mark_delivered(turn_id)

    log_event(
        logger,
        logging.INFO,
        "pma.delivery.discord",
        turn_id=turn_id,
        channel_id=channel_id,
        chunk_count=len(chunks),
    )
    return True


__all__ = ["deliver_pma_output_to_active_sink"]
