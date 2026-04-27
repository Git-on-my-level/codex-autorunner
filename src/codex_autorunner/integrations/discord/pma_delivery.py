"""Discord PMA delivery adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core.chat_bindings import (
    normalize_workspace_path,
    resolve_bound_repo_id,
    resolve_discord_state_path,
    resolve_repo_id_by_workspace_path,
)
from ...core.pma_chat_delivery import PmaChatDeliveryIntent
from ...core.pma_domain.models import PmaDeliveryAttempt
from ...core.text_utils import _normalize_optional_text
from ...core.time_utils import now_iso
from ..chat.pma_delivery import (
    PmaChatDeliveryAdapter,
    PmaChatDeliveryAdapterResult,
    PmaChatDeliveryRecord,
)
from .rendering import chunk_discord_message, format_discord_message
from .state import DiscordStateStore
from .state import OutboxRecord as DiscordOutboxRecord

_DISCORD_MESSAGE_MAX_LEN = 1900


def _build_discord_record_id(
    *,
    correlation_id: str,
    delivery_mode: str,
    channel_id: str,
    index: int,
) -> str:
    digest = hashlib.sha256(
        f"{correlation_id}:{delivery_mode}:{channel_id}:{index}".encode("utf-8")
    ).hexdigest()[:24]
    prefix = "pma-escalation" if delivery_mode == "primary_pma" else "pma-notice"
    return f"{prefix}:{digest}"


def _notification_record(
    *,
    attempt: PmaDeliveryAttempt,
    surface_key: str,
    delivery_record_id: str,
    workspace_root: Optional[str],
) -> PmaChatDeliveryRecord:
    return PmaChatDeliveryRecord(
        delivery_mode=attempt.delivery_mode,
        surface_kind="discord",
        surface_key=surface_key,
        delivery_record_id=delivery_record_id,
        workspace_root=workspace_root,
    )


class DiscordPmaChatDeliveryAdapter(PmaChatDeliveryAdapter):
    @property
    def surface_kind(self) -> str:
        return "discord"

    async def deliver_pma_attempt(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
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
        attempt: PmaDeliveryAttempt,
        hub_root: Path,
        raw_config: Mapping[str, Any],
    ) -> PmaChatDeliveryAdapterResult:
        channel_id = _normalize_optional_text(attempt.target.surface_key)
        if channel_id is None:
            return PmaChatDeliveryAdapterResult(
                route="explicit", targets=0, published=0
            )
        created_at = now_iso()
        store = DiscordStateStore(resolve_discord_state_path(hub_root, raw_config))
        try:
            bindings = await store.list_bindings()
            if not any(
                _normalize_optional_text(binding.get("channel_id")) == channel_id
                for binding in bindings
            ):
                return PmaChatDeliveryAdapterResult(
                    route="explicit",
                    targets=0,
                    published=0,
                )
            chunks = chunk_discord_message(
                format_discord_message(intent.message),
                max_len=_DISCORD_MESSAGE_MAX_LEN,
                with_numbering=False,
            )
            if not chunks:
                chunks = [format_discord_message(intent.message)]
            published = 0
            delivery_records: list[PmaChatDeliveryRecord] = []
            for index, chunk in enumerate(chunks, start=1):
                record_id = _build_discord_record_id(
                    correlation_id=intent.correlation_id,
                    delivery_mode=attempt.delivery_mode,
                    channel_id=channel_id,
                    index=index,
                )
                delivery_records.append(
                    _notification_record(
                        attempt=attempt,
                        surface_key=channel_id,
                        delivery_record_id=record_id,
                        workspace_root=None,
                    )
                )
                if await store.get_outbox(record_id) is not None:
                    continue
                await store.enqueue_outbox(
                    DiscordOutboxRecord(
                        record_id=record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json={"content": chunk},
                        created_at=created_at,
                    )
                )
                published += 1
            return PmaChatDeliveryAdapterResult(
                route="explicit",
                targets=1,
                published=published,
                delivery_records=tuple(delivery_records),
            )
        finally:
            await store.close()

    async def _deliver_bound(
        self,
        intent: PmaChatDeliveryIntent,
        *,
        attempt: PmaDeliveryAttempt,
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
        store = DiscordStateStore(resolve_discord_state_path(hub_root, raw_config))
        try:
            bindings = await store.list_bindings()
            channels: list[str] = []
            for binding in bindings:
                if bool(binding.get("pma_enabled")):
                    continue
                if normalize_workspace_path(
                    binding.get("workspace_path")
                ) != normalize_workspace_path(workspace_root):
                    continue
                binding_repo_id = resolve_bound_repo_id(
                    repo_id=binding.get("repo_id"),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(binding.get("workspace_path"),),
                )
                if normalized_repo_id and binding_repo_id not in {
                    None,
                    normalized_repo_id,
                }:
                    continue
                channel_id = _normalize_optional_text(binding.get("channel_id"))
                if channel_id is None or channel_id in channels:
                    continue
                channels.append(channel_id)
            if not channels:
                return PmaChatDeliveryAdapterResult(
                    route="bound", targets=0, published=0
                )
            chunks = chunk_discord_message(
                format_discord_message(intent.message),
                max_len=_DISCORD_MESSAGE_MAX_LEN,
                with_numbering=False,
            )
            if not chunks:
                chunks = [format_discord_message(intent.message)]
            delivery_records: list[PmaChatDeliveryRecord] = []
            for channel_id in channels:
                targets += 1
                for index, chunk in enumerate(chunks, start=1):
                    record_id = _build_discord_record_id(
                        correlation_id=intent.correlation_id,
                        delivery_mode=attempt.delivery_mode,
                        channel_id=channel_id,
                        index=index,
                    )
                    delivery_records.append(
                        _notification_record(
                            attempt=attempt,
                            surface_key=channel_id,
                            delivery_record_id=record_id,
                            workspace_root=workspace_root,
                        )
                    )
                    if await store.get_outbox(record_id) is not None:
                        continue
                    await store.enqueue_outbox(
                        DiscordOutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                            created_at=created_at,
                        )
                    )
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
        attempt: PmaDeliveryAttempt,
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
        candidates: list[tuple[str, str, Optional[str]]] = []
        store = DiscordStateStore(resolve_discord_state_path(hub_root, raw_config))
        try:
            for binding in await store.list_bindings():
                if not bool(binding.get("pma_enabled")):
                    continue
                binding_repo_id = resolve_bound_repo_id(
                    repo_id=binding.get("repo_id"),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(binding.get("workspace_path"),),
                )
                prev_binding_repo_id = resolve_bound_repo_id(
                    repo_id=binding.get("pma_prev_repo_id"),
                    repo_id_by_workspace=repo_id_by_workspace,
                    workspace_values=(binding.get("pma_prev_workspace_path"),),
                )
                prev_repo_id = _normalize_optional_text(binding.get("pma_prev_repo_id"))
                if repo_id not in {binding_repo_id, prev_repo_id, prev_binding_repo_id}:
                    continue
                channel_id = _normalize_optional_text(binding.get("channel_id"))
                updated_at = _normalize_optional_text(binding.get("updated_at")) or ""
                workspace_path = _normalize_optional_text(
                    binding.get("pma_prev_workspace_path")
                    or binding.get("workspace_path")
                )
                if channel_id is not None:
                    candidates.append(
                        (
                            updated_at,
                            channel_id,
                            workspace_path,
                        )
                    )
            if not candidates:
                return PmaChatDeliveryAdapterResult(
                    route="primary_pma",
                    targets=0,
                    published=0,
                )
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            _updated_at, channel_id, candidate_workspace_root = candidates[0]
            chunks = chunk_discord_message(
                format_discord_message(intent.message),
                max_len=_DISCORD_MESSAGE_MAX_LEN,
                with_numbering=False,
            )
            if not chunks:
                chunks = [format_discord_message(intent.message)]
            published = 0
            delivery_records: list[PmaChatDeliveryRecord] = []
            for index, chunk in enumerate(chunks, start=1):
                record_id = _build_discord_record_id(
                    correlation_id=intent.correlation_id,
                    delivery_mode=attempt.delivery_mode,
                    channel_id=channel_id,
                    index=index,
                )
                delivery_records.append(
                    _notification_record(
                        attempt=attempt,
                        surface_key=channel_id,
                        delivery_record_id=record_id,
                        workspace_root=candidate_workspace_root,
                    )
                )
                if await store.get_outbox(record_id) is not None:
                    continue
                await store.enqueue_outbox(
                    DiscordOutboxRecord(
                        record_id=record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json={"content": chunk},
                        created_at=created_at,
                    )
                )
                published += 1
            return PmaChatDeliveryAdapterResult(
                route="primary_pma",
                targets=1,
                published=published,
                delivery_records=tuple(delivery_records),
            )
        finally:
            await store.close()


__all__ = ["DiscordPmaChatDeliveryAdapter"]
