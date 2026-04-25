"""Telegram PMA delivery adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.chat_bindings import (
    normalize_workspace_path,
    resolve_bound_repo_id,
    resolve_repo_id_by_workspace_path,
    resolve_telegram_state_path,
)
from ...core.pma_chat_delivery import PmaChatDeliveryAttempt, PmaChatDeliveryIntent
from ...core.text_utils import _normalize_optional_text
from ...core.time_utils import now_iso
from ..chat.pma_delivery import (
    PmaChatDeliveryAdapter,
    PmaChatDeliveryAdapterResult,
    PmaChatDeliveryRecord,
)
from .state import OutboxRecord as TelegramOutboxRecord
from .state import TelegramStateStore, parse_topic_key


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
    attempt: PmaChatDeliveryAttempt,
    surface_key: str,
    delivery_record_id: str,
    workspace_root: Optional[str],
) -> PmaChatDeliveryRecord:
    return PmaChatDeliveryRecord(
        delivery_mode=attempt.delivery_mode,
        surface_kind="telegram",
        surface_key=surface_key,
        delivery_record_id=delivery_record_id,
        workspace_root=workspace_root,
    )


class TelegramPmaChatDeliveryAdapter(PmaChatDeliveryAdapter):
    @property
    def surface_kind(self) -> str:
        return "telegram"

    async def deliver_pma_attempt(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaChatDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
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
        return PmaChatDeliveryAdapterResult(
            route=attempt.route,
            targets=0,
            published=0,
        )

    async def _deliver_explicit(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaChatDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
        topic_surface_key = _normalize_optional_text(attempt.target.surface_key)
        if topic_surface_key is None:
            return PmaChatDeliveryAdapterResult(
                route="explicit", targets=0, published=0
            )
        created_at = now_iso()
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            topic = topics.get(topic_surface_key)
            if topic is None:
                return PmaChatDeliveryAdapterResult(
                    route="explicit",
                    targets=0,
                    published=0,
                )
            try:
                chat_id, thread_id, scope = parse_topic_key(topic_surface_key)
            except ValueError:
                return PmaChatDeliveryAdapterResult(
                    route="explicit",
                    targets=0,
                    published=0,
                )
            base_key = f"{chat_id}:{thread_id or 'root'}"
            if scope != await store.get_topic_scope(base_key):
                return PmaChatDeliveryAdapterResult(
                    route="explicit",
                    targets=0,
                    published=0,
                )
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
                return PmaChatDeliveryAdapterResult(
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
            return PmaChatDeliveryAdapterResult(
                route="explicit",
                targets=1,
                published=1,
                delivery_records=(delivery_record,),
            )
        finally:
            await store.close()

    async def _deliver_bound(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaChatDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
        workspace_root = attempt.workspace_root
        if workspace_root is None:
            return PmaChatDeliveryAdapterResult(route="bound", targets=0, published=0)
        normalized_repo_id = _normalize_optional_text(intent.repo_id)
        repo_id_by_workspace = resolve_repo_id_by_workspace_path(hub_root, raw_config)
        created_at = now_iso()
        targets = 0
        published = 0
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            delivery_records: list[PmaChatDeliveryRecord] = []
            for surface_key in sorted(topics):
                topic = topics[surface_key]
                if bool(getattr(topic, "pma_enabled", False)):
                    continue
                try:
                    chat_id, thread_id, scope = parse_topic_key(surface_key)
                except ValueError:
                    continue
                base_key = f"{chat_id}:{thread_id or 'root'}"
                if scope != await store.get_topic_scope(base_key):
                    continue
                if normalize_workspace_path(
                    getattr(topic, "workspace_path", None)
                ) != normalize_workspace_path(workspace_root):
                    continue
                binding_repo_id = resolve_bound_repo_id(
                    repo_id=getattr(topic, "repo_id", None),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(getattr(topic, "workspace_path", None),),
                )
                if normalized_repo_id and binding_repo_id not in {
                    None,
                    normalized_repo_id,
                }:
                    continue
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
                        workspace_root=workspace_root,
                    )
                )
                if await store.get_outbox(record_id) is not None:
                    targets += 1
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
                targets += 1
                published += 1
            return PmaChatDeliveryAdapterResult(
                route="bound",
                targets=targets,
                published=published,
                delivery_records=tuple(delivery_records),
            )
        finally:
            await store.close()

    async def _deliver_primary_pma(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaChatDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
        repo_id = _normalize_optional_text(intent.repo_id)
        if repo_id is None:
            return PmaChatDeliveryAdapterResult(
                route="primary_pma",
                targets=0,
                published=0,
            )
        repo_id_by_workspace = resolve_repo_id_by_workspace_path(hub_root, raw_config)
        created_at = now_iso()
        store = TelegramStateStore(resolve_telegram_state_path(hub_root, raw_config))
        try:
            topics = await store.list_topics()
            for surface_key in sorted(topics):
                topic = topics[surface_key]
                if not bool(getattr(topic, "pma_enabled", False)):
                    continue
                try:
                    chat_id, thread_id, scope = parse_topic_key(surface_key)
                except ValueError:
                    continue
                base_key = f"{chat_id}:{thread_id or 'root'}"
                if scope != await store.get_topic_scope(base_key):
                    continue
                binding_repo_id = resolve_bound_repo_id(
                    repo_id=getattr(topic, "repo_id", None),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(getattr(topic, "workspace_path", None),),
                )
                prev_binding_repo_id = resolve_bound_repo_id(
                    repo_id=getattr(topic, "pma_prev_repo_id", None),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(getattr(topic, "pma_prev_workspace_path", None),),
                )
                prev_repo_id = _normalize_optional_text(
                    getattr(topic, "pma_prev_repo_id", None)
                )
                if repo_id not in {binding_repo_id, prev_repo_id, prev_binding_repo_id}:
                    continue
                record_id = _build_telegram_record_id(
                    correlation_id=intent.correlation_id,
                    delivery_mode=attempt.delivery_mode,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
                workspace_path_raw = _normalize_optional_text(
                    getattr(topic, "pma_prev_workspace_path", None)
                    or getattr(topic, "workspace_path", None)
                )
                delivery_record = _notification_record(
                    attempt=attempt,
                    surface_key=surface_key,
                    delivery_record_id=record_id,
                    workspace_root=workspace_path_raw,
                )
                if await store.get_outbox(record_id) is not None:
                    return PmaChatDeliveryAdapterResult(
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
                return PmaChatDeliveryAdapterResult(
                    route="primary_pma",
                    targets=1,
                    published=1,
                    delivery_records=(delivery_record,),
                )
        finally:
            await store.close()
        return PmaChatDeliveryAdapterResult(
            route="primary_pma",
            targets=0,
            published=0,
        )


__all__ = ["TelegramPmaChatDeliveryAdapter"]
