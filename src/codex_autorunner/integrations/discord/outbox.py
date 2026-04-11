from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from ...core.config import ConfigError, load_repo_config
from ...core.flows import FlowStore
from ...core.flows.archive_helpers import flow_run_archive_root
from .rendering import DISCORD_MAX_MESSAGE_LENGTH, chunk_discord_message
from .state import DiscordStateStore, OutboxRecord

OUTBOX_RETRY_INTERVAL_SECONDS = 5.0
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_IMMEDIATE_RETRY_DELAYS = (0.0, 1.0, 2.0)
_OUTBOX_PROGRESS_KEY = "_codex_autorunner_outbox"
_OUTBOX_PROGRESS_CHUNK_INDEX = "discord_chunk_start_index"

SendMessageFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
DeleteMessageFn = Callable[[str, str], Awaitable[None]]
DeliveredCallback = Callable[[OutboxRecord, Optional[str]], Awaitable[None]]


def _parse_next_attempt_at(next_at_str: Optional[str]) -> Optional[datetime]:
    if not isinstance(next_at_str, str) or not next_at_str:
        return None
    try:
        return datetime.strptime(next_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    current: Optional[BaseException] = exc
    while current is not None:
        retry_attr = getattr(current, "retry_after_seconds", None)
        if isinstance(retry_attr, (int, float)):
            return max(float(retry_attr), 0.0)
        if isinstance(current, httpx.HTTPStatusError):
            header = current.response.headers.get("Retry-After")
            if header:
                try:
                    return max(float(header), 0.0)
                except ValueError:
                    pass
        current = current.__cause__ or current.__context__
    return None


def _discord_chunk_start_index(payload_json: dict[str, Any]) -> int:
    raw_progress = payload_json.get(_OUTBOX_PROGRESS_KEY)
    if not isinstance(raw_progress, dict):
        return 0
    raw_index = raw_progress.get(_OUTBOX_PROGRESS_CHUNK_INDEX)
    if not isinstance(raw_index, int):
        return 0
    return max(raw_index, 0)


def _with_discord_chunk_start_index(
    payload_json: dict[str, Any], start_index: int
) -> dict[str, Any]:
    next_payload = dict(payload_json)
    if start_index <= 0:
        next_payload.pop(_OUTBOX_PROGRESS_KEY, None)
        return next_payload
    raw_progress = next_payload.get(_OUTBOX_PROGRESS_KEY)
    progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
    progress[_OUTBOX_PROGRESS_CHUNK_INDEX] = start_index
    next_payload[_OUTBOX_PROGRESS_KEY] = progress
    return next_payload


def _discord_send_payload(payload_json: dict[str, Any]) -> dict[str, Any]:
    send_payload = dict(payload_json)
    send_payload.pop(_OUTBOX_PROGRESS_KEY, None)
    return send_payload


def _terminal_run_id(record_id: str) -> Optional[str]:
    parts = record_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "terminal":
        return None
    run_id = parts[2].strip()
    return run_id or None


def _has_archived_run_artifacts(workspace_root: Path, run_id: str) -> bool:
    archive_root = flow_run_archive_root(workspace_root, run_id)
    if archive_root.exists() and (archive_root / "archived_tickets").exists():
        return True
    if archive_root.exists() and any(archive_root.glob("archived_runs*")):
        return True
    legacy_root = workspace_root / ".codex-autorunner" / "flows" / run_id
    if not legacy_root.exists():
        return False
    if (legacy_root / "archived_tickets").exists():
        return True
    return any(legacy_root.glob("archived_runs*"))


class DiscordOutboxManager:
    def __init__(
        self,
        store: DiscordStateStore,
        *,
        send_message: SendMessageFn,
        delete_message: Optional[DeleteMessageFn] = None,
        on_delivered: Optional[DeliveredCallback] = None,
        logger: logging.Logger,
        retry_interval_seconds: float = OUTBOX_RETRY_INTERVAL_SECONDS,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
        immediate_retry_delays: tuple[float, ...] = OUTBOX_IMMEDIATE_RETRY_DELAYS,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._send_message = send_message
        self._delete_message = delete_message
        self._on_delivered = on_delivered
        self._logger = logger
        self._retry_interval_seconds = max(retry_interval_seconds, 0.1)
        self._max_attempts = max(max_attempts, 1)
        self._immediate_retry_delays = immediate_retry_delays
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._inflight: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._lock = asyncio.Lock()

    async def run_loop(self) -> None:
        while True:
            await self._sleep(self._retry_interval_seconds)
            try:
                records = await self._store.list_outbox()
                if records:
                    await self._flush(records)
            except (
                RuntimeError,
                OSError,
                sqlite3.Error,
            ) as exc:  # outbox flush must not crash
                self._logger.warning("discord.outbox.flush_failed: %s", exc)

    async def send_with_outbox(self, record: OutboxRecord) -> bool:
        await self._store.enqueue_outbox(record)
        immediate_delays_iter = iter(self._immediate_retry_delays)
        while True:
            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                await self._drop_exhausted(current)
                return False

            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                sleep_duration = (next_at - self._now()).total_seconds()
                if sleep_duration > 0.01:
                    await self._sleep(sleep_duration)

            if await self._attempt_send(current):
                return True

            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= self._max_attempts:
                await self._drop_exhausted(current)
                return False

            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                continue

            try:
                delay = next(immediate_delays_iter)
            except StopIteration:
                return False
            if delay > 0:
                await self._sleep(delay)

    async def _flush(self, records: list[OutboxRecord]) -> None:
        now = self._now()
        for record in records:
            if record.attempts >= self._max_attempts:
                await self._drop_exhausted(record)
                continue
            next_at = _parse_next_attempt_at(record.next_attempt_at)
            if next_at is not None and now < next_at:
                continue
            await self._attempt_send(record)

    async def _attempt_send(self, record: OutboxRecord) -> bool:
        current = await self._store.get_outbox(record.record_id)
        if current is None:
            return False
        if current.attempts >= self._max_attempts:
            await self._drop_exhausted(current)
            return False
        if await self._should_drop_terminal_notification(current):
            await self._store.mark_outbox_delivered(current.record_id)
            self._logger.info(
                "discord.outbox.dropped_stale_terminal record_id=%s",
                current.record_id,
            )
            return False
        if not await self._mark_inflight(current.record_id):
            return False
        try:
            delivered_message_id: Optional[str] = None
            failure_payload_json = current.payload_json
            if current.operation == "send":
                send_payload = _discord_send_payload(current.payload_json)
                payload_content = (
                    send_payload.get("content")
                    if isinstance(send_payload, dict)
                    else None
                )
                if (
                    isinstance(payload_content, str)
                    and len(payload_content) > DISCORD_MAX_MESSAGE_LENGTH
                ):
                    chunk_start_index = _discord_chunk_start_index(current.payload_json)
                    chunks = chunk_discord_message(
                        payload_content,
                        max_len=DISCORD_MAX_MESSAGE_LENGTH,
                        with_numbering=False,
                    )
                    if not chunks:
                        chunks = [payload_content[:DISCORD_MAX_MESSAGE_LENGTH]]
                    if chunk_start_index >= len(chunks):
                        chunk_start_index = max(len(chunks) - 1, 0)
                    last_response: dict[str, Any] = {}
                    for chunk_index in range(chunk_start_index, len(chunks)):
                        chunk_payload = dict(send_payload)
                        chunk = chunks[chunk_index]
                        chunk_payload["content"] = chunk
                        last_response = await self._send_message(
                            current.channel_id, chunk_payload
                        )
                        failure_payload_json = _with_discord_chunk_start_index(
                            current.payload_json, chunk_index + 1
                        )
                    message_id = (
                        last_response.get("id")
                        if isinstance(last_response, dict)
                        else None
                    )
                    delivered_message_id = (
                        message_id if isinstance(message_id, str) else None
                    )
                else:
                    response = await self._send_message(
                        current.channel_id, send_payload
                    )
                    message_id = (
                        response.get("id") if isinstance(response, dict) else None
                    )
                    delivered_message_id = (
                        message_id if isinstance(message_id, str) else None
                    )
            elif current.operation == "delete":
                if (
                    self._delete_message is None
                    or not isinstance(current.message_id, str)
                    or not current.message_id
                ):
                    await self._store.record_outbox_failure(
                        current.record_id,
                        error=(
                            "Unsupported Discord outbox delete operation: "
                            "missing delete handler or message id"
                        ),
                        retry_after_seconds=None,
                    )
                    return False
                await self._delete_message(current.channel_id, current.message_id)
            else:
                await self._store.record_outbox_failure(
                    current.record_id,
                    error=f"Unsupported Discord outbox operation: {current.operation}",
                    retry_after_seconds=None,
                )
                return False
        except Exception as exc:  # retry boundary
            retry_after = _extract_retry_after_seconds(exc)
            await self._store.record_outbox_failure(
                current.record_id,
                error=str(exc),
                retry_after_seconds=retry_after,
                payload_json=failure_payload_json,
            )
            self._logger.warning(
                "discord.outbox.send_failed record_id=%s attempts=%s retry_after=%s error=%s",
                current.record_id,
                current.attempts + 1,
                retry_after,
                exc,
            )
            return False
        finally:
            await self._clear_inflight(current.record_id)

        if self._on_delivered is not None:
            try:
                await self._on_delivered(current, delivered_message_id)
            except (
                RuntimeError,
                TypeError,
                ValueError,
            ):  # callback must not disrupt delivery
                self._logger.warning(
                    "discord.outbox.delivery_callback_failed record_id=%s",
                    current.record_id,
                    exc_info=True,
                )
        await self._store.mark_outbox_delivered(current.record_id)
        self._logger.info("discord.outbox.delivered record_id=%s", current.record_id)
        return True

    async def _drop_exhausted(self, record: OutboxRecord) -> None:
        self._logger.warning(
            "discord.outbox.gave_up record_id=%s attempts=%s error=%s",
            record.record_id,
            record.attempts,
            record.last_error,
        )
        await self._store.mark_outbox_delivered(record.record_id)

    async def _should_drop_terminal_notification(self, record: OutboxRecord) -> bool:
        if record.operation != "send":
            return False
        run_id = _terminal_run_id(record.record_id)
        if run_id is None:
            return False
        try:
            binding = await self._store.get_binding(channel_id=record.channel_id)
        except (sqlite3.Error, RuntimeError):
            return False
        workspace_raw = (
            binding.get("workspace_path") if isinstance(binding, dict) else None
        )
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            return False
        workspace_root = Path(workspace_raw)
        if _has_archived_run_artifacts(workspace_root, run_id):
            return True
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return True
        try:
            durable_writes = load_repo_config(workspace_root).durable_writes
        except ConfigError:
            durable_writes = False
        try:
            with FlowStore(db_path, durable=durable_writes) as store:
                return store.get_flow_run(run_id) is None
        except (sqlite3.Error, OSError):
            return False

    async def _mark_inflight(self, key: str) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    async def _clear_inflight(self, key: str) -> None:
        if self._lock is None:
            return
        async with self._lock:
            self._inflight.discard(key)
