"""Out-of-core registry for PMA chat delivery adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .core.pma_chat_delivery import PmaChatDeliveryIntent
from .core.pma_notification_store import PmaNotificationStore
from .integrations.chat.pma_delivery import (
    PmaChatDeliveryAdapter,
    PmaChatDeliveryAdapterResult,
    PmaChatDeliveryRecord,
)
from .integrations.discord.pma_delivery import DiscordPmaChatDeliveryAdapter
from .integrations.telegram.pma_delivery import TelegramPmaChatDeliveryAdapter

_DEFAULT_PMA_CHAT_DELIVERY_ADAPTERS: dict[str, PmaChatDeliveryAdapter] = {
    "discord": DiscordPmaChatDeliveryAdapter(),
    "telegram": TelegramPmaChatDeliveryAdapter(),
}


def _record_notification_deliveries(
    *,
    hub_root: Path,
    intent: PmaChatDeliveryIntent,
    records: tuple[PmaChatDeliveryRecord, ...],
) -> None:
    if not records:
        return
    notification_store = PmaNotificationStore(hub_root)
    for record in records:
        notification_store.record_notification(
            correlation_id=intent.correlation_id,
            source_kind=intent.source_kind,
            delivery_mode=record.delivery_mode,
            surface_kind=record.surface_kind,
            surface_key=record.surface_key,
            delivery_record_id=record.delivery_record_id,
            repo_id=intent.repo_id,
            workspace_root=record.workspace_root,
            run_id=intent.run_id,
            managed_thread_id=intent.managed_thread_id,
            context=dict(intent.context_payload or {}),
        )


async def dispatch_pma_chat_delivery_intent(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    intent: PmaChatDeliveryIntent,
) -> dict[str, Any]:
    last_result = PmaChatDeliveryAdapterResult(
        route=intent.requested_delivery,
        targets=0,
        published=0,
    )
    for attempt in intent.attempts:
        adapter = _DEFAULT_PMA_CHAT_DELIVERY_ADAPTERS.get(attempt.target.surface_kind)
        if adapter is None:
            last_result = PmaChatDeliveryAdapterResult(
                route=attempt.route,
                targets=0,
                published=0,
            )
            continue
        result = await adapter.deliver_pma_attempt(
            intent,
            attempt=attempt,
            hub_root=hub_root,
            raw_config=raw_config,
        )
        _record_notification_deliveries(
            hub_root=hub_root,
            intent=intent,
            records=result.delivery_records,
        )
        if result.targets > 0:
            return result.to_dict()
        last_result = result
    return last_result.to_dict()


__all__ = ["dispatch_pma_chat_delivery_intent"]
