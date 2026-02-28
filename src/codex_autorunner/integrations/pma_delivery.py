from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..core.locks import file_lock
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
PMA_DELIVERY_MIRROR_REL_PATH = Path(".codex-autorunner/pma/deliveries.jsonl")
PMA_DELIVERY_MIRROR_MAX_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class _TargetDeliveryOutcome:
    success: bool
    chunk_count: int
    error: Optional[str] = None


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


def _resolve_mirror_path(hub_root: Path) -> Path:
    return (hub_root / PMA_DELIVERY_MIRROR_REL_PATH).resolve()


def _outbox_record_id(
    *, turn_id: str, target_delivery_key: str, chunk_index: int
) -> str:
    return f"pma:{turn_id}:{target_delivery_key}:{chunk_index}"


def _dispatch_outbox_record_id(
    *, dispatch_id: str, target_delivery_key: str, chunk_index: int
) -> str:
    return f"pma-dispatch:{dispatch_id}:{target_delivery_key}:{chunk_index}"


def _rotate_jsonl_if_needed(path: Path, *, max_bytes: int) -> None:
    if max_bytes <= 0:
        return
    if not path.exists():
        return
    if path.stat().st_size < max_bytes:
        return
    rotated_path = path.with_suffix(path.suffix + ".1")
    rotated_path.parent.mkdir(parents=True, exist_ok=True)
    if rotated_path.exists():
        rotated_path.unlink()
    path.replace(rotated_path)


def _append_jsonl(path: Path, payload: Mapping[str, Any], *, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        _rotate_jsonl_if_needed(path, max_bytes=max_bytes)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload)) + "\n")


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
) -> _TargetDeliveryOutcome:
    path = _resolve_local_target(target, hub_root=hub_root)
    if path is None:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="invalid_local_target_path",
        )

    payload = {
        "ts": now_iso(),
        "turn_id": turn_id,
        "event_type": event_type,
        "text_preview": assistant_text[:LOCAL_PREVIEW_MAX_CHARS],
        "text_bytes": len(assistant_text.encode("utf-8")),
    }
    _append_jsonl(path, payload, max_bytes=PMA_DELIVERY_MIRROR_MAX_BYTES)
    return _TargetDeliveryOutcome(success=True, chunk_count=1)


def _normalize_dispatch_record(payload: Any) -> Optional[dict[str, Any]]:
    if isinstance(payload, Mapping):
        dispatch_id = payload.get("dispatch_id")
        title = payload.get("title")
        body = payload.get("body")
        priority = payload.get("priority")
        links = payload.get("links")
    else:
        dispatch_id = getattr(payload, "dispatch_id", None)
        title = getattr(payload, "title", None)
        body = getattr(payload, "body", None)
        priority = getattr(payload, "priority", None)
        links = getattr(payload, "links", None)

    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        return None
    normalized_links: list[dict[str, str]] = []
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            label = link.get("label")
            href = link.get("href")
            if not isinstance(label, str) or not label.strip():
                continue
            if not isinstance(href, str) or not href.strip():
                continue
            normalized_links.append({"label": label.strip(), "href": href.strip()})

    return {
        "dispatch_id": dispatch_id.strip(),
        "title": title.strip() if isinstance(title, str) else "",
        "body": body.strip() if isinstance(body, str) else "",
        "priority": priority.strip() if isinstance(priority, str) else "",
        "links": normalized_links,
    }


def _render_dispatch_message(dispatch: Mapping[str, Any]) -> str:
    title = dispatch.get("title") if isinstance(dispatch.get("title"), str) else ""
    priority = (
        dispatch.get("priority") if isinstance(dispatch.get("priority"), str) else ""
    )
    header_title = title or "PMA dispatch"
    header_priority = priority or "info"
    header = f"**PMA dispatch** ({header_priority})\n{header_title}"
    body = dispatch.get("body") if isinstance(dispatch.get("body"), str) else ""
    links = dispatch.get("links")
    link_lines: list[str] = []
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            label = link.get("label")
            href = link.get("href")
            if not isinstance(label, str) or not label.strip():
                continue
            if not isinstance(href, str) or not href.strip():
                continue
            link_lines.append(f"- {label.strip()}: {href.strip()}")
    details = "\n".join(line for line in [body.strip(), "\n".join(link_lines)] if line)
    return f"{header}\n\n{details}" if details else header


def _resolve_dispatch_local_target_path(
    *,
    target: Mapping[str, Any],
    hub_root: Path,
) -> Optional[Path]:
    return _resolve_local_target(target, hub_root=hub_root)


async def _deliver_dispatch_to_local(
    *,
    hub_root: Path,
    target: Mapping[str, Any],
    target_delivery_key: str,
    dispatch: Mapping[str, Any],
    turn_id: str,
    chunk_count: int,
) -> _TargetDeliveryOutcome:
    dispatch_id = dispatch.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="missing_dispatch_id",
        )
    path = _resolve_dispatch_local_target_path(target=target, hub_root=hub_root)
    if path is None:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="invalid_local_target_path",
        )
    payload = {
        "ts": now_iso(),
        "kind": "dispatch",
        "dispatch_id": dispatch_id,
        "turn_id": turn_id,
        "target": target_delivery_key,
        "chunk_count": max(chunk_count, 0),
    }
    _append_jsonl(path, payload, max_bytes=PMA_DELIVERY_MIRROR_MAX_BYTES)
    return _TargetDeliveryOutcome(success=True, chunk_count=1)


async def _deliver_dispatch_to_telegram(
    *,
    hub_root: Path,
    target_delivery_key: str,
    chat_id: int,
    thread_id: Optional[int],
    dispatch: Mapping[str, Any],
    turn_id: str,
    telegram_state_path: Optional[Path] = None,
) -> _TargetDeliveryOutcome:
    dispatch_id = dispatch.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="missing_dispatch_id",
        )
    message = _render_dispatch_message(dispatch)
    chunks = chunk_text(
        message, max_len=TELEGRAM_MAX_MESSAGE_LENGTH, with_numbering=True
    )
    if not chunks:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="empty_chunks",
        )

    resolved_state_path = telegram_state_path or (hub_root / "telegram_state.sqlite3")
    store = TelegramStateStore(resolved_state_path)
    try:
        for idx, chunk in enumerate(chunks, 1):
            record_id = _dispatch_outbox_record_id(
                dispatch_id=dispatch_id,
                target_delivery_key=target_delivery_key,
                chunk_index=idx,
            )
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
    except Exception as exc:
        logger.exception("Failed to enqueue PMA dispatch to Telegram outbox")
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=len(chunks),
            error=str(exc),
        )
    finally:
        await store.close()

    log_event(
        logger,
        logging.INFO,
        "pma.dispatch.delivery.telegram",
        dispatch_id=dispatch_id,
        turn_id=turn_id,
        chat_id=chat_id,
        thread_id=thread_id,
        target=target_delivery_key,
        chunk_count=len(chunks),
    )
    return _TargetDeliveryOutcome(success=True, chunk_count=len(chunks))


async def _deliver_dispatch_to_discord(
    *,
    hub_root: Path,
    target_delivery_key: str,
    channel_id: str,
    dispatch: Mapping[str, Any],
    turn_id: str,
    discord_state_path: Optional[Path] = None,
) -> _TargetDeliveryOutcome:
    dispatch_id = dispatch.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="missing_dispatch_id",
        )
    message = _render_dispatch_message(dispatch)
    chunks = chunk_text(
        message, max_len=DISCORD_MAX_MESSAGE_LENGTH, with_numbering=False
    )
    if not chunks:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="empty_chunks",
        )

    resolved_state_path = discord_state_path or (
        hub_root / ".codex-autorunner" / "discord_state.sqlite3"
    )
    store = DiscordStateStore(resolved_state_path)
    try:
        await store.initialize()
        for idx, chunk in enumerate(chunks, 1):
            record_id = _dispatch_outbox_record_id(
                dispatch_id=dispatch_id,
                target_delivery_key=target_delivery_key,
                chunk_index=idx,
            )
            record = DiscordOutboxRecord(
                record_id=record_id,
                channel_id=channel_id,
                message_id=None,
                operation="send",
                payload_json={"content": chunk},
                created_at=now_iso(),
            )
            await store.enqueue_outbox(record)
    except Exception as exc:
        logger.exception("Failed to enqueue PMA dispatch to Discord outbox")
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=len(chunks),
            error=str(exc),
        )
    finally:
        await store.close()

    log_event(
        logger,
        logging.INFO,
        "pma.dispatch.delivery.discord",
        dispatch_id=dispatch_id,
        turn_id=turn_id,
        channel_id=channel_id,
        target=target_delivery_key,
        chunk_count=len(chunks),
    )
    return _TargetDeliveryOutcome(success=True, chunk_count=len(chunks))


async def deliver_pma_dispatches_to_delivery_targets(
    *,
    hub_root: Path,
    turn_id: str,
    dispatches: Sequence[Any],
    telegram_state_path: Path,
    discord_state_path: Optional[Path] = None,
) -> bool:
    if not isinstance(turn_id, str) or not turn_id:
        return False
    normalized_dispatches = [
        item
        for item in (_normalize_dispatch_record(dispatch) for dispatch in dispatches)
        if isinstance(item, dict)
    ]
    if not normalized_dispatches:
        return False

    state = PmaDeliveryTargetsStore(hub_root).load()
    targets = state.get("targets")
    if not isinstance(targets, list) or not targets:
        return False

    delivered_any = False
    configured_targets = 0
    failed_targets = 0
    delivered_targets = 0

    for target in targets:
        if not isinstance(target, dict):
            continue
        key = target_key(target)
        if not isinstance(key, str):
            continue
        configured_targets += 1

        if target.get("kind") == "web":
            delivered_any = True
            delivered_targets += 1
            continue

        target_failed = False
        for dispatch in normalized_dispatches:
            message = _render_dispatch_message(dispatch)
            expected_chunk_count = 1
            if _resolve_discord_target(target):
                expected_chunk_count = len(
                    chunk_text(
                        message,
                        max_len=DISCORD_MAX_MESSAGE_LENGTH,
                        with_numbering=False,
                    )
                )
            elif _resolve_telegram_target(target) is not None:
                expected_chunk_count = len(
                    chunk_text(
                        message,
                        max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                        with_numbering=True,
                    )
                )

            outcome: _TargetDeliveryOutcome
            if target.get("kind") == "local":
                outcome = await _deliver_dispatch_to_local(
                    hub_root=hub_root,
                    target=target,
                    target_delivery_key=key,
                    dispatch=dispatch,
                    turn_id=turn_id,
                    chunk_count=expected_chunk_count,
                )
            else:
                discord_channel_id = _resolve_discord_target(target)
                if discord_channel_id:
                    outcome = await _deliver_dispatch_to_discord(
                        hub_root=hub_root,
                        target_delivery_key=key,
                        channel_id=discord_channel_id,
                        dispatch=dispatch,
                        turn_id=turn_id,
                        discord_state_path=discord_state_path,
                    )
                else:
                    telegram_target = _resolve_telegram_target(target)
                    if telegram_target is None:
                        outcome = _TargetDeliveryOutcome(
                            success=False,
                            chunk_count=0,
                            error="unsupported_target",
                        )
                    else:
                        chat_id, thread_id = telegram_target
                        outcome = await _deliver_dispatch_to_telegram(
                            hub_root=hub_root,
                            target_delivery_key=key,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            dispatch=dispatch,
                            turn_id=turn_id,
                            telegram_state_path=telegram_state_path,
                        )

            if not outcome.success:
                target_failed = True

        if target_failed:
            failed_targets += 1
            continue

        delivered_any = True
        delivered_targets += 1

    if delivered_any:
        log_event(
            logger,
            logging.INFO,
            "pma.dispatch.delivery.multi_target",
            turn_id=turn_id,
            delivered_targets=delivered_targets,
            configured_targets=configured_targets,
            dispatch_count=len(normalized_dispatches),
        )
    return delivered_any and failed_targets == 0


def _write_delivery_mirror_record(
    *,
    hub_root: Path,
    turn_id: str,
    event_type: Optional[str],
    delivery_targets: list[str],
    chunk_count_by_target: dict[str, int],
    errors: list[dict[str, Any]],
    delivered_targets: list[str],
    skipped_duplicates: list[str],
) -> None:
    mirror_path = _resolve_mirror_path(hub_root)
    payload = {
        "ts": now_iso(),
        "turn_id": turn_id,
        "event_type": event_type,
        "delivery_targets": delivery_targets,
        "delivered_targets": delivered_targets,
        "skipped_duplicates": skipped_duplicates,
        "chunk_count_by_target": chunk_count_by_target,
        "errors": errors,
    }
    _append_jsonl(mirror_path, payload, max_bytes=PMA_DELIVERY_MIRROR_MAX_BYTES)


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
    delivery_target_keys: list[str] = []
    delivered_target_keys: list[str] = []
    skipped_duplicate_keys: list[str] = []
    chunk_count_by_target: dict[str, int] = {}
    errors: list[dict[str, Any]] = []

    for target in targets:
        if not isinstance(target, dict):
            continue
        key = target_key(target)
        if not isinstance(key, str):
            continue
        target_count += 1
        delivery_target_keys.append(key)
        chunk_count_by_target.setdefault(key, 0)

        if str(last_delivery_by_target.get(key) or "") == turn_id:
            skipped_duplicates += 1
            skipped_duplicate_keys.append(key)
            continue

        outcome: _TargetDeliveryOutcome
        if target.get("kind") == "web":
            if target_store.mark_delivered(key, turn_id):
                delivered_targets += 1
                delivered_any = True
                delivered_target_keys.append(key)
            continue

        if target.get("kind") == "local":
            outcome = await _deliver_to_local(
                target=target,
                hub_root=hub_root,
                assistant_text=assistant_text,
                turn_id=turn_id,
                event_type=event_type,
            )
            chunk_count_by_target[key] = max(outcome.chunk_count, 0)
            if outcome.success:
                delivered_any = True
                if target_store.mark_delivered(key, turn_id):
                    delivered_targets += 1
                    delivered_target_keys.append(key)
            else:
                failed_targets += 1
                errors.append(
                    {
                        "target": key,
                        "error": outcome.error or "delivery_failed",
                    }
                )
            continue

        discord_channel_id = _resolve_discord_target(target)
        if discord_channel_id:
            outcome = await _deliver_to_discord(
                hub_root=hub_root,
                channel_id=discord_channel_id,
                target_delivery_key=key,
                assistant_text=assistant_text,
                turn_id=turn_id,
                discord_state_path=discord_state_path,
                event_type=event_type,
            )
            chunk_count_by_target[key] = max(outcome.chunk_count, 0)
            if outcome.success:
                delivered_any = True
                if target_store.mark_delivered(key, turn_id):
                    delivered_targets += 1
                    delivered_target_keys.append(key)
            else:
                failed_targets += 1
                errors.append(
                    {
                        "target": key,
                        "error": outcome.error or "delivery_failed",
                    }
                )
            continue

        target_info = _resolve_telegram_target(target)
        if target_info is None:
            failed_targets += 1
            errors.append(
                {
                    "target": key,
                    "error": "unsupported_target",
                }
            )
            continue
        chat_id, thread_id = target_info
        outcome = await _deliver_to_telegram(
            hub_root=hub_root,
            chat_id=chat_id,
            thread_id=thread_id,
            target_delivery_key=key,
            assistant_text=assistant_text,
            turn_id=turn_id,
            event_type=event_type,
            telegram_state_path=telegram_state_path,
        )
        chunk_count_by_target[key] = max(outcome.chunk_count, 0)
        if outcome.success:
            delivered_any = True
            if target_store.mark_delivered(key, turn_id):
                delivered_targets += 1
                delivered_target_keys.append(key)
        else:
            failed_targets += 1
            errors.append(
                {
                    "target": key,
                    "error": outcome.error or "delivery_failed",
                }
            )

    if target_count > 0:
        _write_delivery_mirror_record(
            hub_root=hub_root,
            turn_id=turn_id,
            event_type=event_type,
            delivery_targets=delivery_target_keys,
            chunk_count_by_target=chunk_count_by_target,
            errors=errors,
            delivered_targets=delivered_target_keys,
            skipped_duplicates=skipped_duplicate_keys,
        )

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
    target_delivery_key: str,
    assistant_text: str,
    turn_id: str,
    event_type: Optional[str] = None,
    telegram_state_path: Optional[Path] = None,
) -> _TargetDeliveryOutcome:
    chunks = chunk_text(
        assistant_text, max_len=TELEGRAM_MAX_MESSAGE_LENGTH, with_numbering=False
    )
    if not chunks:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="empty_chunks",
        )

    resolved_state_path = telegram_state_path or (hub_root / "telegram_state.sqlite3")
    store = TelegramStateStore(resolved_state_path)
    try:
        for idx, chunk in enumerate(chunks, 1):
            record_id = _outbox_record_id(
                turn_id=turn_id,
                target_delivery_key=target_delivery_key,
                chunk_index=idx,
            )
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
    except Exception as exc:
        logger.exception("Failed to enqueue PMA output to Telegram outbox")
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=len(chunks),
            error=str(exc),
        )
    finally:
        await store.close()

    log_event(
        logger,
        logging.INFO,
        "pma.delivery.telegram",
        turn_id=turn_id,
        chat_id=chat_id,
        thread_id=thread_id,
        target=target_delivery_key,
        chunk_count=len(chunks),
        event_type=event_type,
    )
    return _TargetDeliveryOutcome(success=True, chunk_count=len(chunks))


async def _deliver_to_discord(
    *,
    hub_root: Path,
    channel_id: str,
    target_delivery_key: str,
    assistant_text: str,
    turn_id: str,
    discord_state_path: Optional[Path] = None,
    event_type: Optional[str] = None,
) -> _TargetDeliveryOutcome:
    if discord_state_path is None:
        discord_state_path = hub_root / ".codex-autorunner" / "discord_state.sqlite3"

    chunks = chunk_text(
        assistant_text, max_len=DISCORD_MAX_MESSAGE_LENGTH, with_numbering=False
    )
    if not chunks:
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=0,
            error="empty_chunks",
        )

    store = DiscordStateStore(discord_state_path)
    try:
        await store.initialize()
        for idx, chunk in enumerate(chunks, 1):
            record_id = _outbox_record_id(
                turn_id=turn_id,
                target_delivery_key=target_delivery_key,
                chunk_index=idx,
            )
            record = DiscordOutboxRecord(
                record_id=record_id,
                channel_id=channel_id,
                message_id=None,
                operation="send",
                payload_json={"content": chunk},
                created_at=now_iso(),
            )
            await store.enqueue_outbox(record)
    except Exception as exc:
        logger.exception("Failed to enqueue PMA output to Discord outbox")
        return _TargetDeliveryOutcome(
            success=False,
            chunk_count=len(chunks),
            error=str(exc),
        )
    finally:
        await store.close()

    log_event(
        logger,
        logging.INFO,
        "pma.delivery.discord",
        turn_id=turn_id,
        channel_id=channel_id,
        target=target_delivery_key,
        chunk_count=len(chunks),
        event_type=event_type,
    )
    return _TargetDeliveryOutcome(success=True, chunk_count=len(chunks))


__all__ = [
    "deliver_pma_output_to_active_sink",
    "deliver_pma_dispatches_to_delivery_targets",
]
