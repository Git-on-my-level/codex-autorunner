from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ..integrations.telegram.adapter import chunk_message
from ..integrations.telegram.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ..integrations.telegram.state import OutboxRecord, TelegramStateStore
from .pma_sink import PmaActiveSinkStore
from .time_utils import now_iso

logger = logging.getLogger(__name__)


async def deliver_pma_output_to_active_sink(
    *,
    hub_root: Path,
    assistant_text: str,
    turn_id: str,
    lifecycle_event: Optional[dict[str, Any]],
    telegram_state_path: Path,
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
    if sink.get("kind") != "telegram":
        return False

    last_delivery = sink.get("last_delivery_turn_id")
    if isinstance(last_delivery, str) and last_delivery == turn_id:
        return False

    chat_id = sink.get("chat_id")
    thread_id = sink.get("thread_id")
    if not isinstance(chat_id, int):
        return False
    if thread_id is not None and not isinstance(thread_id, int):
        thread_id = None

    chunks = chunk_message(
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


__all__ = ["deliver_pma_output_to_active_sink"]
