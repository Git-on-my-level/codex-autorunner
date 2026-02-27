from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.logging_utils import log_event
from ..core.pma_delivery_targets import PmaDeliveryTargetsStore, target_key
from ..core.time_utils import now_iso
from ..core.utils import is_within
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
LOCAL_PREVIEW_MAX_CHARS = 200


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
    target: Mapping[str, Any],
) -> Optional[tuple[int, Optional[int]]]:
    kind = target.get("kind")
    if kind == "telegram":
        chat_id = target.get("chat_id")
        thread_id = target.get("thread_id")
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        return chat_id, thread_id
    if kind == "chat" and target.get("platform") == "telegram":
        chat_id = _parse_int(target.get("chat_id"))
        if chat_id is None:
            return None
        thread_id = _parse_int(target.get("thread_id"))
        return chat_id, thread_id
    return None


def _resolve_discord_target(
    target: Mapping[str, Any],
) -> Optional[str]:
    kind = target.get("kind")
    platform = target.get("platform")
    if kind == "chat" and platform == "discord":
        channel_id = target.get("chat_id")
        if isinstance(channel_id, str) and channel_id.strip():
            return channel_id.strip()
    return None


def _write_local_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _resolve_local_target(
    target: Mapping[str, Any],
    *,
    hub_root: Path,
) -> Optional[Path]:
    if target.get("kind") != "local":
        return None
    raw_path = target.get("path")
    if not isinstance(raw_path, str):
        return None
    path_text = raw_path.strip()
    if not path_text:
        return None
    configured_path = Path(path_text).expanduser()
    if configured_path.is_absolute():
        resolved = configured_path.resolve()
    else:
        resolved = (hub_root / configured_path).resolve()
    if not is_within(hub_root.resolve(), resolved):
        return None
    return resolved


async def _deliver_to_local(
    *,
    target: Mapping[str, Any],
    hub_root: Path,
    assistant_text: str,
    turn_id: str,
    event_type: Optional[str] = None,
) -> bool:
    path = _resolve_local_target(target, hub_root=hub_root)
    if path is None:
        return False

    payload = {
        "ts": now_iso(),
        "turn_id": turn_id,
        "event_type": event_type,
        "text_preview": assistant_text[:LOCAL_PREVIEW_MAX_CHARS],
        "text_bytes": len(assistant_text.encode("utf-8")),
    }
    _write_local_jsonl(path, payload)
    return True


async def deliver_pma_output_to_active_sink(
    *,
    hub_root: Path,
    assistant_text: str,
    turn_id: str,
    lifecycle_event: Optional[dict[str, Any]],
    telegram_state_path: Path,
    discord_state_path: Optional[Path] = None,
) -> bool:
    if not assistant_text or not assistant_text.strip():
        return False
    if not isinstance(turn_id, str) or not turn_id:
        return False

    event_type = (
        lifecycle_event.get("event_type")
        if isinstance(lifecycle_event, dict)
        and isinstance(lifecycle_event.get("event_type"), str)
        else None
    )

    target_store = PmaDeliveryTargetsStore(hub_root)
    state = target_store.load()
    targets = state.get("targets")
    if not isinstance(targets, list) or not targets:
        return False

    last_delivery_by_target_raw = state.get("last_delivery_by_target")
    last_delivery_by_target: dict[str, Any] = (
        last_delivery_by_target_raw
        if isinstance(last_delivery_by_target_raw, dict)
        else {}
    )

    delivered_any = False
    delivered_targets = 0
    target_count = 0
    failed_targets = 0
    skipped_duplicates = 0

    for target in targets:
        if not isinstance(target, dict):
            continue
        key = target_key(target)
        if not isinstance(key, str):
            continue
        target_count += 1

        if str(last_delivery_by_target.get(key) or "") == turn_id:
            skipped_duplicates += 1
            continue

        if target.get("kind") == "web":
            if target_store.mark_delivered(key, turn_id):
                delivered_targets += 1
                delivered_any = True
            continue

        if target.get("kind") == "local":
            success = await _deliver_to_local(
                target=target,
                hub_root=hub_root,
                assistant_text=assistant_text,
                turn_id=turn_id,
                event_type=event_type,
            )
            if success:
                delivered_any = True
                if target_store.mark_delivered(key, turn_id):
                    delivered_targets += 1
            else:
                failed_targets += 1
            continue

        discord_channel_id = _resolve_discord_target(target)
        if discord_channel_id:
            success = await _deliver_to_discord(
                hub_root=hub_root,
                channel_id=discord_channel_id,
                assistant_text=assistant_text,
                turn_id=turn_id,
                discord_state_path=discord_state_path,
                event_type=event_type,
            )
            if success:
                delivered_any = True
                if target_store.mark_delivered(key, turn_id):
                    delivered_targets += 1
            else:
                failed_targets += 1
            continue

        target_info = _resolve_telegram_target(target)
        if target_info is None:
            continue
        chat_id, thread_id = target_info
        success = await _deliver_to_telegram(
            hub_root=hub_root,
            chat_id=chat_id,
            thread_id=thread_id,
            assistant_text=assistant_text,
            turn_id=turn_id,
            event_type=event_type,
            telegram_state_path=telegram_state_path,
        )
        if success:
            delivered_any = True
            if target_store.mark_delivered(key, turn_id):
                delivered_targets += 1
        else:
            failed_targets += 1

    if target_count > 0 and skipped_duplicates == target_count:
        log_event(
            logger,
            logging.INFO,
            "pma.delivery.skip_duplicate_turn",
            turn_id=turn_id,
            event_type=event_type,
        )
        return False

    if delivered_any:
        log_event(
            logger,
            logging.INFO,
            "pma.delivery.multi_target",
            turn_id=turn_id,
            delivered_targets=delivered_targets,
            configured_targets=target_count,
            event_type=event_type,
        )

    return delivered_any and failed_targets == 0


async def _deliver_to_telegram(
    *,
    hub_root: Path,
    chat_id: int,
    thread_id: Optional[int],
    assistant_text: str,
    turn_id: str,
    event_type: Optional[str] = None,
    telegram_state_path: Optional[Path] = None,
) -> bool:
    chunks = chunk_text(
        assistant_text, max_len=TELEGRAM_MAX_MESSAGE_LENGTH, with_numbering=False
    )
    if not chunks:
        return False

    resolved_state_path = telegram_state_path or (hub_root / "telegram_state.sqlite3")
    store = TelegramStateStore(resolved_state_path)
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

    log_event(
        logger,
        logging.INFO,
        "pma.delivery.telegram",
        turn_id=turn_id,
        chat_id=chat_id,
        thread_id=thread_id,
        chunk_count=len(chunks),
        event_type=event_type,
    )
    return True


async def _deliver_to_discord(
    *,
    hub_root: Path,
    channel_id: str,
    assistant_text: str,
    turn_id: str,
    discord_state_path: Optional[Path] = None,
    event_type: Optional[str] = None,
) -> bool:
    if discord_state_path is None:
        discord_state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    chunks = chunk_text(
        assistant_text, max_len=DISCORD_MAX_MESSAGE_LENGTH, with_numbering=False
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

    log_event(
        logger,
        logging.INFO,
        "pma.delivery.discord",
        turn_id=turn_id,
        channel_id=channel_id,
        chunk_count=len(chunks),
        event_type=event_type,
    )
    return True


__all__ = ["deliver_pma_output_to_active_sink"]
