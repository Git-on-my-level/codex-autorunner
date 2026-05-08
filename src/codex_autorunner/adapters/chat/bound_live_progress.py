from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ...core.chat_bindings import (
    resolve_discord_state_path,
    resolve_telegram_state_path,
)
from ...core.orchestration import OrchestrationBindingStore
from ...core.pma_notification_store import PmaNotificationStore
from ...core.ports.run_event import RunEvent
from ...core.time_utils import now_iso
from ..discord.rendering import truncate_for_discord
from ..discord.rest import DiscordRestClient
from ..discord.state import DiscordStateStore
from ..discord.state import OutboxRecord as DiscordOutboxRecord
from ..telegram.adapter import TelegramBotClient
from ..telegram.outbox import _outbox_key as telegram_outbox_key
from ..telegram.state import OutboxRecord as TelegramOutboxRecord
from ..telegram.state import TelegramStateStore, parse_topic_key
from .agents import DEFAULT_CHAT_AGENT_MODELS
from .managed_thread_progress_projector import ManagedThreadProgressProjector
from .managed_thread_turns import (
    ManagedThreadExecutionHooks,
    ManagedThreadFinalizationResult,
)
from .progress_primitives import TurnProgressTracker
from .turn_metrics import _extract_context_usage_percent

logger = logging.getLogger(__name__)

_DISCORD_MAX_PROGRESS_LEN = 1900
_TELEGRAM_MAX_PROGRESS_LEN = 4096
_PROGRESS_SOURCE_KIND = "managed_thread_live_progress"
_EDIT_OPERATION = "edit"
_DELETE_OPERATION = "delete"


@dataclass
class BoundChatLiveProgressSession:
    adapters: tuple["_BaseBoundProgressAdapter", ...]
    tracker: TurnProgressTracker
    projector: ManagedThreadProgressProjector
    max_length: int

    @property
    def enabled(self) -> bool:
        return bool(self.adapters)

    @property
    def surface_targets(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (adapter.surface_kind, adapter.surface_key) for adapter in self.adapters
        )

    async def start(self) -> bool:
        if not self.adapters:
            return False
        self.projector.mark_working(force=True)
        rendered = self.projector.render(
            max_length=self.max_length,
            now=time.monotonic(),
        )
        published = False
        for adapter in self.adapters:
            try:
                if await adapter.publish(rendered):
                    published = True
            except Exception:
                logger.exception(
                    "Failed to publish bound chat live progress start (surface_kind=%s, surface_key=%s)",
                    adapter.surface_kind,
                    adapter.surface_key,
                )
        if published:
            self.projector.note_rendered(rendered, now=time.monotonic())
        return published

    async def apply_run_events(self, events: list[RunEvent]) -> None:
        if not self.adapters:
            return
        for event in events:
            if hasattr(event, "usage") and isinstance(
                getattr(event, "usage", None), dict
            ):
                self.projector.note_context_usage(
                    _extract_context_usage_percent(getattr(event, "usage", None))
                )
            outcome = self.projector.apply_run_event(event)
            if not outcome.changed:
                continue
            rendered = self.projector.render(
                max_length=self.max_length,
                now=time.monotonic(),
                render_mode=outcome.render_mode,
            )
            published = False
            for adapter in self.adapters:
                try:
                    if await adapter.publish(rendered):
                        published = True
                except Exception:
                    logger.exception(
                        "Failed to publish bound chat live progress update (surface_kind=%s, surface_key=%s)",
                        adapter.surface_kind,
                        adapter.surface_key,
                    )
            if published:
                self.projector.note_rendered(rendered, now=time.monotonic())

    async def finalize(
        self,
        *,
        status: str,
        failure_message: Optional[str] = None,
    ) -> None:
        if not self.adapters:
            return
        normalized = str(status or "").strip().lower()
        if normalized == "ok":
            for adapter in self.adapters:
                try:
                    await adapter.complete_success()
                except Exception:
                    logger.exception(
                        "Failed to retire bound chat live progress success state (surface_kind=%s, surface_key=%s)",
                        adapter.surface_kind,
                        adapter.surface_key,
                    )
            return
        if normalized == "interrupted":
            self.tracker.set_label("cancelled")
            self.tracker.note_error(failure_message or "Turn interrupted.")
        else:
            self.tracker.set_label("failed")
            self.tracker.note_error(failure_message or "Turn failed.")
        rendered = self.projector.render(
            max_length=self.max_length,
            now=time.monotonic(),
            render_mode="final",
        )
        for adapter in self.adapters:
            try:
                await adapter.complete_with_message(rendered)
            except Exception:
                logger.exception(
                    "Failed to publish bound chat live progress terminal state (surface_kind=%s, surface_key=%s)",
                    adapter.surface_kind,
                    adapter.surface_key,
                )

    async def close(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.close()
            except Exception:
                logger.exception(
                    "Failed to close bound chat live progress adapter (surface_kind=%s, surface_key=%s)",
                    adapter.surface_kind,
                    adapter.surface_key,
                )


@dataclass
class BoundChatQueueExecutionController:
    hooks: ManagedThreadExecutionHooks
    _completed_surface_targets: dict[str, tuple[tuple[str, str], ...]]

    def surface_targets_for(
        self,
        managed_turn_id: str,
    ) -> tuple[tuple[str, str], ...]:
        return self._completed_surface_targets.get(managed_turn_id, ())

    def clear_surface_targets(self, managed_turn_id: str) -> None:
        self._completed_surface_targets.pop(managed_turn_id, None)


def resolve_bound_chat_queue_progress_context(
    owner: Any,
    *,
    fallback_root: Path,
) -> tuple[Path, Mapping[str, Any]]:
    config = getattr(owner, "_config", None)
    hub_root = getattr(config, "root", None)
    raw_config = getattr(config, "raw", None)
    resolved_raw: dict[str, Any] = (
        dict(raw_config) if isinstance(raw_config, Mapping) else {}
    )
    if config is not None:
        _maybe_overlay_live_surface_config(
            resolved_raw,
            section="discord_bot",
            bot_token=getattr(config, "bot_token", None),
            state_file=getattr(config, "state_file", None),
            marker_fields=("application_id", "app_id_env"),
            config=config,
        )
        _maybe_overlay_live_surface_config(
            resolved_raw,
            section="telegram_bot",
            bot_token=getattr(config, "bot_token", None),
            state_file=getattr(config, "state_file", None),
            marker_fields=("allowed_chat_ids", "api_base_url"),
            config=config,
        )
    return (
        hub_root if isinstance(hub_root, Path) else Path(fallback_root),
        resolved_raw,
    )


def _maybe_overlay_live_surface_config(
    raw_config: dict[str, Any],
    *,
    section: str,
    bot_token: Any,
    state_file: Any,
    marker_fields: tuple[str, ...],
    config: Any,
) -> None:
    has_marker = any(hasattr(config, field) for field in marker_fields)
    token = str(bot_token or "").strip()
    state_path = str(state_file).strip() if state_file is not None else ""
    if not has_marker or (not token and not state_path):
        return
    existing = raw_config.get(section)
    section_config = dict(existing) if isinstance(existing, Mapping) else {}
    if token and not str(section_config.get("bot_token") or "").strip():
        section_config["bot_token"] = token
    if state_path and not str(section_config.get("state_file") or "").strip():
        section_config["state_file"] = state_path
    if token and "enabled" not in section_config:
        section_config["enabled"] = True
    raw_config[section] = section_config


class _BaseBoundProgressAdapter:
    def __init__(
        self,
        *,
        hub_root: Path,
        managed_thread_id: str,
        managed_turn_id: str,
        surface_key: str,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._managed_thread_id = managed_thread_id
        self._managed_turn_id = managed_turn_id
        self._surface_key = surface_key
        self._surface_scope_id = _bound_progress_surface_scope_id(
            surface_kind=self.surface_kind,
            surface_key=surface_key,
        )
        self._notifications = PmaNotificationStore(self._hub_root)

    @property
    def send_record_id(self) -> str:
        return bound_chat_progress_send_record_id(
            surface_kind=self.surface_kind,
            surface_key=self._surface_key,
            managed_thread_id=self._managed_thread_id,
            managed_turn_id=self._managed_turn_id,
        )

    @property
    def edit_operation_id(self) -> str:
        return bound_chat_progress_edit_operation_id(
            surface_kind=self.surface_kind,
            surface_key=self._surface_key,
            managed_thread_id=self._managed_thread_id,
            managed_turn_id=self._managed_turn_id,
        )

    @property
    def delete_record_id(self) -> str:
        return bound_chat_progress_delete_record_id(
            surface_kind=self.surface_kind,
            surface_key=self._surface_key,
            managed_thread_id=self._managed_thread_id,
            managed_turn_id=self._managed_turn_id,
        )

    @property
    def surface_key(self) -> str:
        return self._surface_key

    def _record_notification(self) -> None:
        self._notifications.record_notification(
            correlation_id=(
                "managed-thread-progress:"
                f"{self._managed_thread_id}:{self._managed_turn_id}:{self._surface_scope_id}"
            ),
            source_kind=_PROGRESS_SOURCE_KIND,
            delivery_mode="bound",
            surface_kind=self.surface_kind,
            surface_key=self._surface_key,
            delivery_record_id=self.send_record_id,
            managed_thread_id=self._managed_thread_id,
            context={"managed_turn_id": self._managed_turn_id},
        )

    def _delivered_anchor_id(self) -> Optional[str]:
        record = self._notifications.get_by_delivery_record_id(self.send_record_id)
        if record is None:
            return None
        anchor_id = str(record.delivered_message_id or "").strip()
        return anchor_id or None

    async def publish(self, text: str) -> bool:
        self._record_notification()
        anchor_id = self._delivered_anchor_id()
        if anchor_id is None:
            return await self._upsert_pending_send(text)
        return await self._enqueue_edit(anchor_id, text)

    async def _retire_outbox_records(self) -> None:
        store = getattr(self, "_store", None)
        if store is None:
            return
        await retire_bound_chat_progress_outbox_records(
            store=store,
            surface_kind=self.surface_kind,
            surface_key=self._surface_key,
            managed_thread_id=self._managed_thread_id,
            managed_turn_id=self._managed_turn_id,
        )

    async def complete_success(self) -> None:
        anchor_id = self._delivered_anchor_id()
        if anchor_id is None:
            await self._delete_pending_send()
            return
        await self._retire_outbox_records()
        await self._enqueue_delete(anchor_id)

    async def complete_with_message(self, text: str) -> None:
        self._record_notification()
        anchor_id = self._delivered_anchor_id()
        if anchor_id is None:
            await self._upsert_pending_send(text)
            return
        await self._retire_outbox_records()
        await self._enqueue_edit(anchor_id, text)

    async def close(self) -> None:
        return None

    @property
    def surface_kind(self) -> str:
        raise NotImplementedError

    async def _upsert_pending_send(self, text: str) -> bool:
        raise NotImplementedError

    async def _enqueue_edit(self, anchor_id: str, text: str) -> bool:
        raise NotImplementedError

    async def _enqueue_delete(self, anchor_id: str) -> None:
        raise NotImplementedError

    async def _delete_pending_send(self) -> None:
        raise NotImplementedError


class _DiscordBoundProgressAdapter(_BaseBoundProgressAdapter):
    def __init__(
        self,
        *,
        hub_root: Path,
        raw_config: Mapping[str, Any],
        managed_thread_id: str,
        managed_turn_id: str,
        channel_id: str,
    ) -> None:
        super().__init__(
            hub_root=hub_root,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            surface_key=channel_id,
        )
        self._store = DiscordStateStore(
            resolve_discord_state_path(hub_root, raw_config)
        )
        self._raw_config = dict(raw_config)
        self._rest: DiscordRestClient | None = None

    @property
    def surface_kind(self) -> str:
        return "discord"

    async def _rest_client(self) -> DiscordRestClient | None:
        if self._rest is not None:
            return self._rest
        discord_config = self._raw_config.get("discord_bot")
        if not isinstance(discord_config, Mapping):
            return None
        bot_token = str(discord_config.get("bot_token") or "").strip()
        if not bot_token:
            return None
        self._rest = DiscordRestClient(bot_token=bot_token)
        return self._rest

    async def _upsert_pending_send(self, text: str) -> bool:
        rest = await self._rest_client()
        payload = {
            "content": truncate_for_discord(text, max_len=_DISCORD_MAX_PROGRESS_LEN)
        }
        if rest is not None:
            existing = await self._store.get_outbox(self.send_record_id)
            if existing is not None:
                rest = None
        if rest is not None:
            try:
                response = await rest.create_channel_message(
                    channel_id=self._surface_key,
                    payload=payload,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Discord send failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                delivered_message_id = str(
                    response.get("id") if isinstance(response, Mapping) else ""
                ).strip()
                if delivered_message_id:
                    self._notifications.mark_delivered(
                        delivery_record_id=self.send_record_id,
                        delivered_message_id=delivered_message_id,
                    )
                    return True
        record = DiscordOutboxRecord(
            record_id=self.send_record_id,
            channel_id=self._surface_key,
            message_id=None,
            operation="send",
            payload_json=payload,
            created_at=now_iso(),
        )
        await self._store.enqueue_outbox(record)
        return True

    async def _enqueue_edit(self, anchor_id: str, text: str) -> bool:
        rest = await self._rest_client()
        payload = {
            "content": truncate_for_discord(text, max_len=_DISCORD_MAX_PROGRESS_LEN)
        }
        if rest is not None:
            if any(
                record.operation_id == self.edit_operation_id
                for record in await self._store.list_outbox()
            ):
                rest = None
        if rest is not None:
            try:
                await rest.edit_channel_message(
                    channel_id=self._surface_key,
                    message_id=anchor_id,
                    payload=payload,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Discord edit failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                return True
        record = DiscordOutboxRecord(
            record_id=f"{self.edit_operation_id}:{uuid.uuid4().hex[:8]}",
            channel_id=self._surface_key,
            message_id=anchor_id,
            operation=_EDIT_OPERATION,
            payload_json=payload,
            created_at=now_iso(),
            operation_id=self.edit_operation_id,
        )
        await self._store.enqueue_outbox(record)
        return True

    async def _enqueue_delete(self, anchor_id: str) -> None:
        rest = await self._rest_client()
        if rest is not None:
            existing_delete = await self._store.get_outbox(self.delete_record_id)
            if existing_delete is not None:
                rest = None
        if rest is not None:
            try:
                await rest.delete_channel_message(
                    channel_id=self._surface_key,
                    message_id=anchor_id,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Discord delete failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                return
        record = DiscordOutboxRecord(
            record_id=self.delete_record_id,
            channel_id=self._surface_key,
            message_id=anchor_id,
            operation=_DELETE_OPERATION,
            payload_json={},
            created_at=now_iso(),
        )
        await self._store.enqueue_outbox(record)

    async def _delete_pending_send(self) -> None:
        await self._store.mark_outbox_delivered(self.send_record_id)

    async def close(self) -> None:
        if self._rest is not None:
            await self._rest.close()
        await self._store.close()


class _TelegramBoundProgressAdapter(_BaseBoundProgressAdapter):
    def __init__(
        self,
        *,
        hub_root: Path,
        raw_config: Mapping[str, Any],
        managed_thread_id: str,
        managed_turn_id: str,
        topic_surface_key: str,
    ) -> None:
        super().__init__(
            hub_root=hub_root,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            surface_key=topic_surface_key,
        )
        chat_id, thread_id, _scope = parse_topic_key(topic_surface_key)
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._store = TelegramStateStore(
            resolve_telegram_state_path(hub_root, raw_config)
        )
        self._raw_config = dict(raw_config)
        self._bot: TelegramBotClient | None = None

    @property
    def surface_kind(self) -> str:
        return "telegram"

    async def _bot_client(self) -> TelegramBotClient | None:
        if self._bot is not None:
            return self._bot
        telegram_config = self._raw_config.get("telegram_bot")
        if not isinstance(telegram_config, Mapping):
            return None
        bot_token = str(telegram_config.get("bot_token") or "").strip()
        if not bot_token:
            return None
        self._bot = TelegramBotClient(bot_token, logger=logger)
        return self._bot

    async def _upsert_pending_send(self, text: str) -> bool:
        bot = await self._bot_client()
        bounded_text = text[:_TELEGRAM_MAX_PROGRESS_LEN]
        if bot is not None:
            existing = await self._store.get_outbox(self.send_record_id)
            if existing is not None:
                bot = None
        if bot is not None:
            try:
                response = await bot.send_message(
                    self._chat_id,
                    bounded_text,
                    message_thread_id=self._thread_id,
                    reply_to_message_id=None,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Telegram send failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                delivered_message_id = (
                    response.get("message_id")
                    if isinstance(response, Mapping)
                    else None
                )
                if isinstance(delivered_message_id, int):
                    self._notifications.mark_delivered(
                        delivery_record_id=self.send_record_id,
                        delivered_message_id=delivered_message_id,
                    )
                    return True
        record = TelegramOutboxRecord(
            record_id=self.send_record_id,
            chat_id=self._chat_id,
            thread_id=self._thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text=bounded_text,
            created_at=now_iso(),
            operation="send",
            message_id=None,
        )
        await self._store.enqueue_outbox(record)
        return True

    async def _enqueue_edit(self, anchor_id: str, text: str) -> bool:
        try:
            message_id = int(anchor_id)
        except (TypeError, ValueError):
            return False
        bounded_text = text[:_TELEGRAM_MAX_PROGRESS_LEN]
        bot = await self._bot_client()
        if bot is not None:
            if any(
                record.operation_id == self.edit_operation_id
                for record in await self._store.list_outbox()
            ):
                bot = None
        if bot is not None:
            try:
                edit_ok = await bot.edit_message_text(
                    self._chat_id,
                    message_id,
                    bounded_text,
                    message_thread_id=self._thread_id,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Telegram edit failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                return bool(edit_ok)
        record = TelegramOutboxRecord(
            record_id=f"{self.edit_operation_id}:{uuid.uuid4().hex[:8]}",
            chat_id=self._chat_id,
            thread_id=self._thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text=bounded_text,
            created_at=now_iso(),
            operation=_EDIT_OPERATION,
            message_id=message_id,
            outbox_key=telegram_outbox_key(
                self._chat_id,
                self._thread_id,
                message_id,
                _EDIT_OPERATION,
            ),
            operation_id=self.edit_operation_id,
        )
        await self._store.enqueue_outbox(record)
        return True

    async def _enqueue_delete(self, anchor_id: str) -> None:
        try:
            message_id = int(anchor_id)
        except (TypeError, ValueError):
            return
        bot = await self._bot_client()
        if bot is not None:
            existing_delete = await self._store.get_outbox(self.delete_record_id)
            if existing_delete is not None:
                bot = None
        if bot is not None:
            try:
                delete_ok = await bot.delete_message(
                    self._chat_id,
                    message_id,
                    message_thread_id=self._thread_id,
                )
            except Exception:
                logger.warning(
                    "Bound chat live progress direct Telegram delete failed; falling back to outbox",
                    exc_info=True,
                )
            else:
                if delete_ok:
                    return
        record = TelegramOutboxRecord(
            record_id=self.delete_record_id,
            chat_id=self._chat_id,
            thread_id=self._thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text="",
            created_at=now_iso(),
            operation=_DELETE_OPERATION,
            message_id=message_id,
            outbox_key=telegram_outbox_key(
                self._chat_id,
                self._thread_id,
                message_id,
                _DELETE_OPERATION,
            ),
        )
        await self._store.enqueue_outbox(record)

    async def _delete_pending_send(self) -> None:
        await self._store.delete_outbox(self.send_record_id)

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.close()
        await self._store.close()


def _bound_progress_surface_scope_id(*, surface_kind: str, surface_key: str) -> str:
    digest = hashlib.sha256(f"{surface_kind}:{surface_key}".encode("utf-8")).hexdigest()
    return digest[:12]


def bound_chat_progress_send_record_id(
    *,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> str:
    return (
        f"managed-thread-progress:{surface_kind}:"
        f"{_bound_progress_surface_scope_id(surface_kind=surface_kind, surface_key=surface_key)}:"
        f"{managed_thread_id}:{managed_turn_id}:send"
    )


def bound_chat_progress_edit_operation_id(
    *,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> str:
    return (
        f"managed-thread-progress:{surface_kind}:"
        f"{_bound_progress_surface_scope_id(surface_kind=surface_kind, surface_key=surface_key)}:"
        f"{managed_thread_id}:{managed_turn_id}:edit"
    )


def bound_chat_progress_delete_record_id(
    *,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> str:
    return (
        f"managed-thread-progress:{surface_kind}:"
        f"{_bound_progress_surface_scope_id(surface_kind=surface_kind, surface_key=surface_key)}:"
        f"{managed_thread_id}:{managed_turn_id}:delete"
    )


async def retire_bound_chat_progress_outbox_records(
    *,
    store: Any,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> str:
    send_record_id = bound_chat_progress_send_record_id(
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
    )
    await store.delete_outbox(send_record_id)
    await store.delete_outbox(
        bound_chat_progress_delete_record_id(
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        )
    )
    edit_operation_id = bound_chat_progress_edit_operation_id(
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
    )
    list_outbox = getattr(store, "list_outbox", None)
    if callable(list_outbox):
        for record in await list_outbox():
            if getattr(record, "operation_id", None) != edit_operation_id:
                continue
            record_id = str(getattr(record, "record_id", "") or "").strip()
            if record_id:
                await store.delete_outbox(record_id)
    return send_record_id


def build_bound_chat_progress_cleanup_metadata(
    *,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> dict[str, str]:
    return {
        "kind": _PROGRESS_SOURCE_KIND,
        "surface_kind": surface_kind,
        "surface_key": surface_key,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "progress_send_record_id": bound_chat_progress_send_record_id(
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        ),
    }


def mark_bound_chat_progress_delivered(
    *,
    hub_root: Path,
    delivery_record_id: str,
    delivered_message_id: Any,
) -> None:
    PmaNotificationStore(hub_root).mark_delivered(
        delivery_record_id=delivery_record_id,
        delivered_message_id=delivered_message_id,
    )


def bound_chat_progress_delivered_message_id(
    *,
    hub_root: Path,
    delivery_record_id: str,
) -> Optional[str]:
    conversation = PmaNotificationStore(hub_root).get_by_delivery_record_id(
        delivery_record_id
    )
    if conversation is None:
        return None
    delivered_message_id = str(conversation.delivered_message_id or "").strip()
    return delivered_message_id or None


def _build_bound_progress_adapter(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> _BaseBoundProgressAdapter | None:
    if surface_kind == "discord" and surface_key:
        return _DiscordBoundProgressAdapter(
            hub_root=hub_root,
            raw_config=raw_config,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            channel_id=surface_key,
        )
    if surface_kind == "telegram" and surface_key:
        return _TelegramBoundProgressAdapter(
            hub_root=hub_root,
            raw_config=raw_config,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            topic_surface_key=surface_key,
        )
    return None


async def cleanup_bound_chat_live_progress_success(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
) -> None:
    adapter = _build_bound_progress_adapter(
        hub_root=hub_root,
        raw_config=raw_config,
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
    )
    if adapter is None:
        return
    try:
        await adapter.complete_success()
    finally:
        await adapter.close()


async def cleanup_bound_chat_live_progress_failure(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    managed_turn_id: str,
    failure_message: str,
) -> None:
    adapter = _build_bound_progress_adapter(
        hub_root=hub_root,
        raw_config=raw_config,
        surface_kind=surface_kind,
        surface_key=surface_key,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
    )
    if adapter is None:
        return
    tracker = TurnProgressTracker(
        started_at=time.monotonic(),
        agent="agent",
        model="default",
        label="failed",
        max_actions=1,
        max_output_chars=(
            _DISCORD_MAX_PROGRESS_LEN
            if surface_kind == "discord"
            else _TELEGRAM_MAX_PROGRESS_LEN
        ),
    )
    tracker.note_error(failure_message or "Turn failed.")
    projector = ManagedThreadProgressProjector(
        tracker,
        min_render_interval_seconds=0.0,
        heartbeat_interval_seconds=5.0,
    )
    rendered = projector.render(
        max_length=tracker.max_output_chars,
        now=time.monotonic(),
        render_mode="final",
    )
    try:
        await adapter.complete_with_message(rendered)
    finally:
        await adapter.close()


def build_bound_chat_live_progress_session(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    agent: str,
    model: Optional[str],
    surface_targets: Optional[tuple[tuple[str, str], ...]] = None,
) -> BoundChatLiveProgressSession:
    adapters: list[_BaseBoundProgressAdapter] = []
    max_length = _DISCORD_MAX_PROGRESS_LEN
    targets = bound_chat_live_progress_targets(
        hub_root=hub_root,
        managed_thread_id=managed_thread_id,
        surface_targets=surface_targets,
    )
    for surface_kind, surface_key in targets:
        try:
            adapter = _build_bound_progress_adapter(
                hub_root=hub_root,
                raw_config=raw_config,
                surface_kind=surface_kind,
                surface_key=surface_key,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
            )
        except Exception:
            logger.exception(
                "Failed to build bound chat live progress adapter (surface_kind=%s, surface_key=%s)",
                surface_kind,
                surface_key,
            )
            continue
        if adapter is None:
            continue
        adapters.append(adapter)
        if surface_kind == "telegram":
            max_length = max(max_length, _TELEGRAM_MAX_PROGRESS_LEN)
        else:
            max_length = max(max_length, _DISCORD_MAX_PROGRESS_LEN)
    tracker = TurnProgressTracker(
        started_at=time.monotonic(),
        agent=agent,
        model=model or DEFAULT_CHAT_AGENT_MODELS.get(agent, "default"),
        label="working",
        max_actions=25,
        max_output_chars=max_length,
    )
    projector = ManagedThreadProgressProjector(
        tracker,
        min_render_interval_seconds=0.25,
        heartbeat_interval_seconds=5.0,
    )
    return BoundChatLiveProgressSession(
        adapters=tuple(adapters),
        tracker=tracker,
        projector=projector,
        max_length=max_length,
    )


def bound_chat_live_progress_targets(
    *,
    hub_root: Path,
    managed_thread_id: str,
    surface_targets: Optional[tuple[tuple[str, str], ...]] = None,
) -> tuple[tuple[str, str], ...]:
    if surface_targets is not None:
        return tuple(
            (
                str(surface_kind).strip(),
                str(surface_key).strip(),
            )
            for surface_kind, surface_key in surface_targets
            if str(surface_kind).strip() and str(surface_key).strip()
        )
    binding_store = OrchestrationBindingStore(hub_root)
    bindings = sorted(
        (
            binding
            for binding in binding_store.list_bindings(
                thread_target_id=managed_thread_id,
                include_disabled=False,
                limit=1000,
            )
            if binding.surface_kind in {"discord", "telegram"}
        ),
        key=lambda binding: (
            str(binding.surface_kind or ""),
            str(binding.surface_key or ""),
        ),
    )
    return tuple(
        (
            str(binding.surface_kind or "").strip(),
            str(binding.surface_key or "").strip(),
        )
        for binding in bindings
        if str(binding.surface_kind or "").strip()
        and str(binding.surface_key or "").strip()
    )


def build_bound_chat_queue_execution_controller(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    surface_targets: Optional[tuple[tuple[str, str], ...]] = None,
    surface_target_resolver: Optional[
        Callable[[Any], tuple[tuple[str, str], ...]]
    ] = None,
    base_hooks: Optional[ManagedThreadExecutionHooks] = None,
    on_progress_session_started: Optional[Callable[[Any], object]] = None,
    retain_completed_surface_targets: bool = False,
) -> BoundChatQueueExecutionController:
    current_session: Optional[BoundChatLiveProgressSession] = None
    current_turn_id: Optional[str] = None
    completed_surface_targets: dict[str, tuple[tuple[str, str], ...]] = {}
    base = base_hooks or ManagedThreadExecutionHooks()

    async def _on_started(started: Any) -> None:
        nonlocal current_session
        nonlocal current_turn_id
        if base.on_execution_started is not None:
            result = base.on_execution_started(started)
            if inspect.isawaitable(result):
                await result
        started_execution = getattr(started, "execution", None)
        started_thread = getattr(started, "thread", None)
        started_request = getattr(started, "request", None)
        current_turn_id = str(
            getattr(started_execution, "execution_id", "") or ""
        ).strip()
        try:
            current_session = build_bound_chat_live_progress_session(
                hub_root=hub_root,
                raw_config=raw_config,
                managed_thread_id=managed_thread_id,
                managed_turn_id=current_turn_id,
                agent=str(getattr(started_thread, "agent_id", "") or "agent"),
                model=getattr(started_request, "model", None),
                surface_targets=(
                    surface_target_resolver(started)
                    if surface_target_resolver is not None
                    else surface_targets
                ),
            )
            published = await current_session.start()
            if published and on_progress_session_started is not None:
                result = on_progress_session_started(started)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            logger.exception(
                "Failed to start bound chat live progress session (managed_thread_id=%s, managed_turn_id=%s)",
                managed_thread_id,
                current_turn_id,
            )
            if current_session is not None:
                try:
                    await current_session.close()
                except Exception:
                    logger.exception(
                        "Failed to close bound chat live progress session after startup error (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        current_turn_id,
                    )
            current_session = None

    async def _on_progress(run_event: RunEvent) -> None:
        if current_session is not None:
            await current_session.apply_run_events([run_event])
        if base.on_progress_event is not None:
            await base.on_progress_event(run_event)

    async def _on_finalized(
        started: Any,
        finalized: ManagedThreadFinalizationResult,
    ) -> None:
        nonlocal current_session
        nonlocal current_turn_id
        session = current_session
        turn_id = current_turn_id
        current_session = None
        current_turn_id = None
        if session is not None:
            if isinstance(finalized, Mapping):
                finalized_status = finalized.get("status")
                finalized_error = finalized.get("error")
            else:
                finalized_status = getattr(finalized, "status", None)
                finalized_error = getattr(finalized, "error", None)
            normalized_status = str(finalized_status or "").strip().lower()
            if normalized_status == "ok":
                if retain_completed_surface_targets and turn_id:
                    completed_surface_targets[turn_id] = session.surface_targets
            else:
                await session.finalize(
                    status=str(finalized_status or "").strip() or "error",
                    failure_message=finalized_error,
                )
            await session.close()
        if base.on_execution_finalized is not None:
            result = base.on_execution_finalized(started, finalized)
            if inspect.isawaitable(result):
                await result

    async def _on_error(started: Any, exc: BaseException) -> None:
        nonlocal current_session
        nonlocal current_turn_id
        session = current_session
        current_session = None
        current_turn_id = None
        if session is not None:
            try:
                if not isinstance(exc, asyncio.CancelledError):
                    await session.finalize(
                        status="error",
                        failure_message=str(exc) or None,
                    )
            finally:
                await session.close()
        if base.on_execution_error is not None:
            result = base.on_execution_error(started, exc)
            if inspect.isawaitable(result):
                await result

    async def _on_finished(started: Any) -> None:
        nonlocal current_session
        nonlocal current_turn_id
        session = current_session
        current_session = None
        current_turn_id = None
        if session is not None:
            await session.close()
        if base.on_execution_finished is not None:
            result = base.on_execution_finished(started)
            if inspect.isawaitable(result):
                await result

    return BoundChatQueueExecutionController(
        hooks=ManagedThreadExecutionHooks(
            on_execution_started=_on_started,
            on_execution_finished=_on_finished,
            on_execution_finalized=_on_finalized,
            on_execution_error=_on_error,
            on_progress_event=_on_progress,
        ),
        _completed_surface_targets=completed_surface_targets,
    )


__all__ = [
    "BoundChatQueueExecutionController",
    "bound_chat_live_progress_targets",
    "BoundChatLiveProgressSession",
    "bound_chat_progress_delete_record_id",
    "bound_chat_progress_delivered_message_id",
    "bound_chat_progress_edit_operation_id",
    "build_bound_chat_queue_execution_controller",
    "build_bound_chat_progress_cleanup_metadata",
    "build_bound_chat_live_progress_session",
    "bound_chat_progress_send_record_id",
    "cleanup_bound_chat_live_progress_failure",
    "cleanup_bound_chat_live_progress_success",
    "mark_bound_chat_progress_delivered",
    "resolve_bound_chat_queue_progress_context",
    "retire_bound_chat_progress_outbox_records",
]
