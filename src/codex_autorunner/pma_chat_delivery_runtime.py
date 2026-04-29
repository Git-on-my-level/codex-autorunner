"""Out-of-core registry for PMA chat delivery adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .core.chat_bindings import (
    normalize_workspace_path,
    orchestration_surface_targets_for_thread,
    preferred_non_pma_chat_notification_source_for_workspace,
    resolve_bound_repo_id,
    resolve_discord_state_path,
    resolve_repo_id_by_workspace_path,
    resolve_telegram_state_path,
)
from .core.pma_chat_delivery import PmaChatDeliveryIntent
from .core.pma_notification_store import PmaNotificationStore
from .core.text_utils import _normalize_optional_text
from .integrations.chat.bound_live_progress import (
    build_bound_chat_live_progress_session,
)
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


def _ordered_surface_kinds_for_bound_progress(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    workspace_root: Path,
) -> tuple[str, ...]:
    preferred_source = preferred_non_pma_chat_notification_source_for_workspace(
        hub_root=hub_root,
        raw_config=raw_config,
        workspace_root=workspace_root,
    )
    if preferred_source in {"discord", "telegram"}:
        ordered_sources = [preferred_source]
        ordered_sources.extend(
            source for source in ("discord", "telegram") if source != preferred_source
        )
        return tuple(ordered_sources)
    return ("discord", "telegram")


async def _resolve_bound_progress_targets(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    workspace_root: Optional[Path],
    repo_id: Optional[str],
) -> tuple[tuple[str, str], ...]:
    normalized_repo_id = _normalize_optional_text(repo_id)
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _append_target(surface_kind: str, surface_key: str) -> None:
        if surface_kind not in {"discord", "telegram"} or not surface_key:
            return
        pair = (surface_kind, surface_key)
        if pair in seen:
            return
        seen.add(pair)
        targets.append(pair)

    for sk, skey in orchestration_surface_targets_for_thread(
        hub_root=hub_root, thread_target_id=managed_thread_id
    ):
        _append_target(sk, skey)

    if workspace_root is None:
        return tuple(targets)

    repo_id_by_workspace = resolve_repo_id_by_workspace_path(hub_root, raw_config)
    for surface_kind in _ordered_surface_kinds_for_bound_progress(
        hub_root=hub_root,
        raw_config=raw_config,
        workspace_root=workspace_root,
    ):
        if surface_kind == "discord":
            from .integrations.discord.state import DiscordStateStore

            discord_store = DiscordStateStore(
                resolve_discord_state_path(hub_root, raw_config)
            )
            try:
                for legacy_binding in await discord_store.list_bindings():
                    if bool(legacy_binding.get("pma_enabled")):
                        continue
                    if normalize_workspace_path(
                        legacy_binding.get("workspace_path")
                    ) != normalize_workspace_path(workspace_root):
                        continue
                    binding_repo_id = resolve_bound_repo_id(
                        repo_id=legacy_binding.get("repo_id"),
                        repo_id_by_workspace=repo_id_by_workspace,
                        workspace_values=(legacy_binding.get("workspace_path"),),
                    )
                    if normalized_repo_id and binding_repo_id not in {
                        None,
                        normalized_repo_id,
                    }:
                        continue
                    channel_id = _normalize_optional_text(
                        legacy_binding.get("channel_id")
                    )
                    if channel_id is not None:
                        _append_target("discord", channel_id)
            finally:
                await discord_store.close()
        elif surface_kind == "telegram":
            from .integrations.telegram.state import TelegramStateStore, parse_topic_key

            telegram_store = TelegramStateStore(
                resolve_telegram_state_path(hub_root, raw_config)
            )
            try:
                for surface_key, topic in sorted(
                    (await telegram_store.list_topics()).items()
                ):
                    if bool(getattr(topic, "pma_enabled", False)):
                        continue
                    try:
                        chat_id, thread_id, scope = parse_topic_key(surface_key)
                    except ValueError:
                        continue
                    base_key = f"{chat_id}:{thread_id or 'root'}"
                    if scope != await telegram_store.get_topic_scope(base_key):
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
                    _append_target("telegram", surface_key)
            finally:
                await telegram_store.close()

    return tuple(targets)


async def start_bound_chat_live_progress_for_thread(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    agent: str,
    model: Optional[str],
    workspace_root: Optional[Path] = None,
    repo_id: Optional[str] = None,
) -> dict[str, Any]:
    targets = await _resolve_bound_progress_targets(
        hub_root=hub_root,
        raw_config=raw_config,
        managed_thread_id=managed_thread_id,
        workspace_root=workspace_root,
        repo_id=repo_id,
    )
    if not targets:
        return {"targets": 0, "published": 0}
    session = build_bound_chat_live_progress_session(
        hub_root=hub_root,
        raw_config=raw_config,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        agent=agent,
        model=model,
        surface_targets=targets,
    )
    try:
        published = await session.start()
    finally:
        await session.close()
    return {
        "targets": len(session.surface_targets),
        "published": 1 if published else 0,
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


__all__ = [
    "dispatch_pma_chat_delivery_intent",
    "start_bound_chat_live_progress_for_thread",
]
