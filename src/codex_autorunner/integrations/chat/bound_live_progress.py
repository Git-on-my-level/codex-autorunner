from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.chat_bindings import (
    resolve_discord_state_path,
    resolve_telegram_state_path,
)
from ...core.orchestration import OrchestrationBindingStore
from ...core.pma_notification_store import PmaNotificationStore
from ...core.ports.run_event import RunEvent
from ...core.time_utils import now_iso
from ..discord.outbox import DiscordOutboxManager
from ..discord.rendering import truncate_for_discord
from ..discord.rest import DiscordRestClient
from ..discord.state import DiscordStateStore
from ..discord.state import OutboxRecord as DiscordOutboxRecord
from ..telegram.adapter import TelegramBotClient
from ..telegram.outbox import TelegramOutboxManager
from ..telegram.outbox import _outbox_key as telegram_outbox_key
from ..telegram.state import OutboxRecord as TelegramOutboxRecord
from ..telegram.state import TelegramStateStore, parse_topic_key
from .managed_thread_progress_projector import ManagedThreadProgressProjector
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

    async def start(self) -> None:
        if not self.adapters:
            return
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
        return (
            f"managed-thread-progress:{self.surface_kind}:"
            f"{self._surface_scope_id}:"
            f"{self._managed_thread_id}:{self._managed_turn_id}:edit"
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

    async def complete_success(self) -> None:
        anchor_id = self._delivered_anchor_id()
        if anchor_id is None:
            await self._delete_pending_send()
            return
        await self._enqueue_delete(anchor_id)

    async def complete_with_message(self, text: str) -> None:
        self._record_notification()
        anchor_id = self._delivered_anchor_id()
        if anchor_id is None:
            await self._upsert_pending_send(text)
            return
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
        self._outbox: DiscordOutboxManager | None = None
        self._rest: DiscordRestClient | None = None

    @property
    def surface_kind(self) -> str:
        return "discord"

    async def _manager(self) -> DiscordOutboxManager | None:
        if self._outbox is not None:
            return self._outbox
        discord_config = self._raw_config.get("discord_bot")
        if not isinstance(discord_config, Mapping):
            return None
        bot_token = str(discord_config.get("bot_token") or "").strip()
        if not bot_token:
            return None
        self._rest = DiscordRestClient(bot_token=bot_token)

        async def _on_delivered(
            record: DiscordOutboxRecord,
            delivered_message_id: Optional[str],
        ) -> None:
            if record.record_id != self.send_record_id:
                return
            self._notifications.mark_delivered(
                delivery_record_id=self.send_record_id,
                delivered_message_id=delivered_message_id,
            )

        async def _send(channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert self._rest is not None
            return await self._rest.create_channel_message(
                channel_id=channel_id,
                payload=payload,
            )

        async def _edit(
            channel_id: str,
            message_id: str,
            payload: dict[str, Any],
        ) -> None:
            assert self._rest is not None
            await self._rest.edit_channel_message(
                channel_id=channel_id,
                message_id=message_id,
                payload=payload,
            )

        async def _delete(channel_id: str, message_id: str) -> None:
            assert self._rest is not None
            await self._rest.delete_channel_message(
                channel_id=channel_id,
                message_id=message_id,
            )

        outbox = DiscordOutboxManager(
            self._store,
            send_message=_send,
            edit_message=_edit,
            delete_message=_delete,
            on_delivered=_on_delivered,
            logger=logger,
        )
        outbox.start()
        self._outbox = outbox
        return outbox

    async def _upsert_pending_send(self, text: str) -> bool:
        record = DiscordOutboxRecord(
            record_id=self.send_record_id,
            channel_id=self._surface_key,
            message_id=None,
            operation="send",
            payload_json={
                "content": truncate_for_discord(text, max_len=_DISCORD_MAX_PROGRESS_LEN)
            },
            created_at=now_iso(),
        )
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return True
        return await manager.send_with_outbox(record)

    async def _enqueue_edit(self, anchor_id: str, text: str) -> bool:
        record = DiscordOutboxRecord(
            record_id=f"{self.edit_operation_id}:{uuid.uuid4().hex[:8]}",
            channel_id=self._surface_key,
            message_id=anchor_id,
            operation=_EDIT_OPERATION,
            payload_json={
                "content": truncate_for_discord(text, max_len=_DISCORD_MAX_PROGRESS_LEN)
            },
            created_at=now_iso(),
            operation_id=self.edit_operation_id,
        )
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return True
        return await manager.send_with_outbox(record)

    async def _enqueue_delete(self, anchor_id: str) -> None:
        record = DiscordOutboxRecord(
            record_id=(
                f"managed-thread-progress:{self.surface_kind}:"
                f"{self._surface_scope_id}:"
                f"{self._managed_thread_id}:{self._managed_turn_id}:delete"
            ),
            channel_id=self._surface_key,
            message_id=anchor_id,
            operation=_DELETE_OPERATION,
            payload_json={},
            created_at=now_iso(),
        )
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return
        await manager.send_with_outbox(record)

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
        self._outbox: TelegramOutboxManager | None = None

    @property
    def surface_kind(self) -> str:
        return "telegram"

    async def _manager(self) -> TelegramOutboxManager | None:
        if self._outbox is not None:
            return self._outbox
        telegram_config = self._raw_config.get("telegram_bot")
        if not isinstance(telegram_config, Mapping):
            return None
        bot_token = str(telegram_config.get("bot_token") or "").strip()
        if not bot_token:
            return None
        self._bot = TelegramBotClient(bot_token, logger=logger)

        async def _on_delivered(
            record: TelegramOutboxRecord,
            delivered_message_id: Optional[int],
        ) -> None:
            if record.record_id != self.send_record_id:
                return
            self._notifications.mark_delivered(
                delivery_record_id=self.send_record_id,
                delivered_message_id=delivered_message_id,
            )

        async def _send(
            chat_id: int,
            text: str,
            *,
            thread_id: Optional[int] = None,
            reply_to: Optional[int] = None,
            overflow_mode_override: Optional[str] = None,
        ) -> Optional[int]:
            _ = overflow_mode_override
            assert self._bot is not None
            response = await self._bot.send_message(
                chat_id,
                text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
            raw_message_id = (
                response.get("message_id") if isinstance(response, Mapping) else None
            )
            return int(raw_message_id) if isinstance(raw_message_id, int) else None

        async def _edit(
            chat_id: int,
            message_id: int,
            text: str,
            *,
            message_thread_id: Optional[int] = None,
        ) -> bool:
            assert self._bot is not None
            result = await self._bot.edit_message_text(
                chat_id,
                message_id,
                text,
                message_thread_id=message_thread_id,
            )
            return bool(result)

        async def _delete(
            chat_id: int,
            message_id: int,
            thread_id: Optional[int],
        ) -> bool:
            assert self._bot is not None
            return await self._bot.delete_message(
                chat_id,
                message_id,
                message_thread_id=thread_id,
            )

        outbox = TelegramOutboxManager(
            self._store,
            send_message=_send,
            edit_message_text=_edit,
            delete_message=_delete,
            on_delivered=_on_delivered,
            logger=logger,
        )
        outbox.start()
        self._outbox = outbox
        return outbox

    async def _upsert_pending_send(self, text: str) -> bool:
        record = TelegramOutboxRecord(
            record_id=self.send_record_id,
            chat_id=self._chat_id,
            thread_id=self._thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text=text[:_TELEGRAM_MAX_PROGRESS_LEN],
            created_at=now_iso(),
            operation="send",
            message_id=None,
        )
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return True
        return await manager.send_message_with_outbox(record)

    async def _enqueue_edit(self, anchor_id: str, text: str) -> bool:
        try:
            message_id = int(anchor_id)
        except (TypeError, ValueError):
            return False
        record = TelegramOutboxRecord(
            record_id=f"{self.edit_operation_id}:{uuid.uuid4().hex[:8]}",
            chat_id=self._chat_id,
            thread_id=self._thread_id,
            reply_to_message_id=None,
            placeholder_message_id=None,
            text=text[:_TELEGRAM_MAX_PROGRESS_LEN],
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
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return True
        return await manager.send_message_with_outbox(record)

    async def _enqueue_delete(self, anchor_id: str) -> None:
        try:
            message_id = int(anchor_id)
        except (TypeError, ValueError):
            return
        record = TelegramOutboxRecord(
            record_id=(
                f"managed-thread-progress:{self.surface_kind}:"
                f"{self._surface_scope_id}:"
                f"{self._managed_thread_id}:{self._managed_turn_id}:delete"
            ),
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
        manager = await self._manager()
        if manager is None:
            await self._store.enqueue_outbox(record)
            return
        await manager.send_message_with_outbox(record)

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


def build_bound_chat_live_progress_session(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    agent: str,
    model: Optional[str],
) -> BoundChatLiveProgressSession:
    binding_store = OrchestrationBindingStore(hub_root)
    adapters: list[_BaseBoundProgressAdapter] = []
    max_length = _DISCORD_MAX_PROGRESS_LEN
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
    for binding in bindings:
        surface_kind = str(binding.surface_kind or "").strip()
        surface_key = str(binding.surface_key or "").strip()
        adapter = _build_bound_progress_adapter(
            hub_root=hub_root,
            raw_config=raw_config,
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        )
        if adapter is None:
            continue
        adapters.append(adapter)
        if surface_kind == "telegram":
            max_length = max(max_length, _TELEGRAM_MAX_PROGRESS_LEN)
        else:
            max_length = _DISCORD_MAX_PROGRESS_LEN
    tracker = TurnProgressTracker(
        started_at=time.monotonic(),
        agent=agent,
        model=model or "default",
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


__all__ = [
    "BoundChatLiveProgressSession",
    "bound_chat_progress_delivered_message_id",
    "build_bound_chat_progress_cleanup_metadata",
    "build_bound_chat_live_progress_session",
    "bound_chat_progress_send_record_id",
    "cleanup_bound_chat_live_progress_success",
    "mark_bound_chat_progress_delivered",
]
