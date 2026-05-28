"""Telegram PMA delivery adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.chat_bindings import (
    resolve_repo_id_by_workspace_path,
    resolve_telegram_state_path,
)
from ...core.chat_delivery import ChatDeliveryIntent
from ...core.pma_domain.models import PmaDeliveryAttempt
from ...core.text_utils import _normalize_optional_text
from ...core.time_utils import now_iso
from ..chat.pma_delivery import (
    ChatDeliveryAdapter,
    ChatDeliveryAdapterResult,
    ChatDeliveryRecord,
)
from ..chat.pma_delivery_targets import (
    ChatSurfaceBinding,
    select_bound_pma_targets,
    select_explicit_pma_target,
    select_primary_pma_target,
)
from .state import OutboxRecord as TelegramOutboxRecord
from .state import TelegramStateStore, parse_topic_key
from .state_types import TelegramTopicRecord


def _build_telegram_record_id(
    *,
    correlation_id: str,
    delivery_mode: str,
    chat_id: int,
    thread_id: Optional[int],
) -> str:
    digest = hashlib.sha256(
        f"{correlation_id}:{delivery_mode}:{chat_id}:{thread_id or 'root'}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    prefix = "pma-escalation" if delivery_mode == "primary_pma" else "pma-notice"
    return f"{prefix}:{digest}"


def _notification_record(
    *,
    attempt: PmaDeliveryAttempt,
    surface_key: str,
    delivery_record_id: str,
    workspace_root: Optional[str],
) -> ChatDeliveryRecord:
    return ChatDeliveryRecord(
        delivery_mode=attempt.delivery_mode,
        surface_kind="telegram",
        surface_key=surface_key,
        delivery_record_id=delivery_record_id,
        workspace_root=workspace_root,
    )


async def _telegram_surface_bindings_and_targets(
    store: TelegramStateStore,
    topics: Mapping[str, TelegramTopicRecord],
) -> tuple[
    tuple[ChatSurfaceBinding, ...],
    dict[str, tuple[int, Optional[int]]],
]:
    projected: list[ChatSurfaceBinding] = []
    parsed_targets: dict[str, tuple[int, Optional[int]]] = {}
    for surface_key in sorted(topics):
        topic = topics[surface_key]
        try:
            chat_id, thread_id, scope = parse_topic_key(surface_key)
        except ValueError:
            continue
        base_key = f"{chat_id}:{thread_id or 'root'}"
        if scope != await store.get_topic_scope(base_key):
            continue
        parsed_targets[surface_key] = (chat_id, thread_id)
        projected.append(
            ChatSurfaceBinding(
                surface_key=surface_key,
                workspace_path=_normalize_optional_text(
                    getattr(topic, "workspace_path", None)
                ),
                repo_id=_normalize_optional_text(getattr(topic, "repo_id", None)),
                is_primary_pma=bool(getattr(topic, "pma_enabled", False)),
                previous_workspace_path=_normalize_optional_text(
                    getattr(topic, "pma_prev_workspace_path", None)
                ),
                previous_repo_id=_normalize_optional_text(
                    getattr(topic, "pma_prev_repo_id", None)
                ),
            )
        )
    return tuple(projected), parsed_targets


class TelegramChatDeliveryAdapter(ChatDeliveryAdapter):
    @property
    def surface_kind(self) -> str:
        return "telegram"

    async def deliver_pma_attempt(
        self,
        intent: ChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> ChatDeliveryAdapterResult:
        if attempt.route == "explicit":
            return await self._deliver_explicit(
                intent,
                attempt=attempt,
                hub_root=hub_root,
                raw_config=raw_config,
            )
        if attempt.route == "bound":
            return await self._deliver_bound(
                intent,
                attempt=attempt,
                hub_root=hub_root,
                raw_config=raw_config,
            )
        if attempt.route == "primary_pma":
            return await self._deliver_primary_pma(
                intent,
                attempt=attempt,
                hub_root=hub_root,
                raw_config=raw_config,
            )
        return ChatDeliveryAdapterResult(
            route=attempt.route,
            targets=0,
            published=0,
        )

    async def _deliver_explicit(
        self,
        intent: ChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> ChatDeliveryAdapterResult:
        topic_surface_key = _normalize_optional_text(attempt.target.surface_key)
        if topic_surface_key is None:
            return ChatDeliveryAdapterResult(route="explicit", targets=0, published=0)
        created_at = now_iso()
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            bindings, parsed_targets = await _telegram_surface_bindings_and_targets(
                store, topics
            )
            target = select_explicit_pma_target(
                surface_key=topic_surface_key,
                bindings=bindings,
            )
            if target is None:
                return ChatDeliveryAdapterResult(
                    route="explicit",
                    targets=0,
                    published=0,
                )
            chat_id, thread_id = parsed_targets[target.surface_key]
            record_id = _build_telegram_record_id(
                correlation_id=intent.correlation_id,
                delivery_mode=attempt.delivery_mode,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            delivery_record = _notification_record(
                attempt=attempt,
                surface_key=topic_surface_key,
                delivery_record_id=record_id,
                workspace_root=None,
            )
            if await store.get_outbox(record_id) is not None:
                return ChatDeliveryAdapterResult(
                    route="explicit",
                    targets=1,
                    published=0,
                    delivery_records=(delivery_record,),
                )
            await store.enqueue_outbox(
                TelegramOutboxRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=None,
                    placeholder_message_id=None,
                    text=intent.message,
                    created_at=created_at,
                    operation="send",
                    message_id=None,
                    outbox_key=(
                        f"pma-notice:{intent.correlation_id}:{topic_surface_key}:send"
                    ),
                )
            )
            return ChatDeliveryAdapterResult(
                route="explicit",
                targets=1,
                published=1,
                delivery_records=(delivery_record,),
            )
        finally:
            await store.close()

    async def _deliver_bound(
        self,
        intent: ChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> ChatDeliveryAdapterResult:
        workspace_root = attempt.workspace_root
        repo_id_by_workspace = resolve_repo_id_by_workspace_path(hub_root, raw_config)
        created_at = now_iso()
        published = 0
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            bindings, parsed_targets = await _telegram_surface_bindings_and_targets(
                store, topics
            )
            targets = select_bound_pma_targets(
                workspace_root=workspace_root,
                repo_id=intent.repo_id,
                bindings=bindings,
                repo_id_by_workspace=repo_id_by_workspace,
            )
            delivery_records: list[ChatDeliveryRecord] = []
            for target in targets:
                surface_key = target.surface_key
                chat_id, thread_id = parsed_targets[surface_key]
                record_id = _build_telegram_record_id(
                    correlation_id=intent.correlation_id,
                    delivery_mode=attempt.delivery_mode,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
                delivery_records.append(
                    _notification_record(
                        attempt=attempt,
                        surface_key=surface_key,
                        delivery_record_id=record_id,
                        workspace_root=target.workspace_root,
                    )
                )
                if await store.get_outbox(record_id) is not None:
                    continue
                await store.enqueue_outbox(
                    TelegramOutboxRecord(
                        record_id=record_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        reply_to_message_id=None,
                        placeholder_message_id=None,
                        text=intent.message,
                        created_at=created_at,
                        operation="send",
                        message_id=None,
                        outbox_key=(
                            f"pma-notice:{intent.correlation_id}:{surface_key}:send"
                        ),
                    )
                )
                published += 1
            return ChatDeliveryAdapterResult(
                route="bound",
                targets=len(targets),
                published=published,
                delivery_records=tuple(delivery_records),
            )
        finally:
            await store.close()

    async def _deliver_primary_pma(
        self,
        intent: ChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> ChatDeliveryAdapterResult:
        repo_id_by_workspace = resolve_repo_id_by_workspace_path(hub_root, raw_config)
        created_at = now_iso()
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            bindings, parsed_targets = await _telegram_surface_bindings_and_targets(
                store, topics
            )
            target = select_primary_pma_target(
                repo_id=intent.repo_id,
                bindings=bindings,
                repo_id_by_workspace=repo_id_by_workspace,
            )
            if target is not None:
                surface_key = target.surface_key
                chat_id, thread_id = parsed_targets[surface_key]
                record_id = _build_telegram_record_id(
                    correlation_id=intent.correlation_id,
                    delivery_mode=attempt.delivery_mode,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
                delivery_record = _notification_record(
                    attempt=attempt,
                    surface_key=surface_key,
                    delivery_record_id=record_id,
                    workspace_root=target.workspace_root,
                )
                if await store.get_outbox(record_id) is not None:
                    return ChatDeliveryAdapterResult(
                        route="primary_pma",
                        targets=1,
                        published=0,
                        delivery_records=(delivery_record,),
                    )
                await store.enqueue_outbox(
                    TelegramOutboxRecord(
                        record_id=record_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        reply_to_message_id=None,
                        placeholder_message_id=None,
                        text=intent.message,
                        created_at=created_at,
                        operation="send",
                        message_id=None,
                        outbox_key=(
                            f"pma-escalation:{intent.correlation_id}:{surface_key}:send"
                        ),
                    )
                )
                return ChatDeliveryAdapterResult(
                    route="primary_pma",
                    targets=1,
                    published=1,
                    delivery_records=(delivery_record,),
                )
        finally:
            await store.close()
        return ChatDeliveryAdapterResult(
            route="primary_pma",
            targets=0,
            published=0,
        )


__all__ = ["TelegramChatDeliveryAdapter"]
