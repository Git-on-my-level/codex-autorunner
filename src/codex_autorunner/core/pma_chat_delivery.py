from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from ..manifest import load_manifest
from .chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
    preferred_non_pma_chat_notification_source_for_workspace,
)
from .config import load_hub_config
from .pma_notification_store import PmaNotificationStore
from .time_utils import now_iso
from .utils import canonicalize_path

logger = logging.getLogger(__name__)

_DISCORD_MESSAGE_MAX_LEN = 1900


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


def _resolve_state_path(
    hub_root: Path, raw_config: dict[str, Any], *, section: str, default_state_file: str
) -> Path:
    section_cfg = raw_config.get(section) if isinstance(raw_config, dict) else {}
    if not isinstance(section_cfg, dict):
        section_cfg = {}
    state_file = section_cfg.get("state_file")
    if not isinstance(state_file, str) or not state_file.strip():
        state_file = default_state_file
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = hub_root / state_path
    return state_path.resolve()


def _binding_matches_workspace(binding_workspace: Any, workspace_root: Path) -> bool:
    if not isinstance(binding_workspace, str) or not binding_workspace.strip():
        return False
    try:
        return canonicalize_path(Path(binding_workspace)) == canonicalize_path(
            workspace_root
        )
    except Exception:
        return False


def _repo_id_by_workspace_path(hub_root: Path) -> dict[str, str]:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    if not manifest_path.exists():
        return {}
    try:
        loaded = load_manifest(manifest_path, hub_root)
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for repo in loaded.repos:
        try:
            workspace_path = canonicalize_path(hub_root / repo.path)
        except Exception:
            continue
        mapping[str(workspace_path)] = repo.id
    return mapping


def _resolve_repo_id(
    *,
    repo_id: Any,
    workspace_path: Any,
    repo_id_by_workspace: dict[str, str],
) -> Optional[str]:
    normalized_repo_id = _normalize_optional_text(repo_id)
    if normalized_repo_id:
        return normalized_repo_id
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return None
    try:
        return repo_id_by_workspace.get(str(canonicalize_path(Path(workspace_path))))
    except Exception:
        return None


def _notification_context_payload(
    *,
    message: str,
    correlation_id: str,
    source_kind: str,
    context_payload: Optional[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(context_payload or {})
    payload.setdefault("message", message)
    payload.setdefault("correlation_id", correlation_id)
    payload.setdefault("source_kind", source_kind)
    return payload


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


def _record_notification_delivery(
    notification_store: PmaNotificationStore,
    *,
    correlation_id: str,
    source_kind: str,
    delivery_mode: str,
    surface_kind: str,
    surface_key: str,
    delivery_record_id: str,
    repo_id: Optional[str],
    workspace_root: Optional[Path],
    run_id: Optional[str],
    managed_thread_id: Optional[str],
    context_payload: Optional[dict[str, Any]],
) -> None:
    notification_store.record_notification(
        correlation_id=correlation_id,
        source_kind=source_kind,
        delivery_mode=delivery_mode,
        surface_kind=surface_kind,
        surface_key=surface_key,
        delivery_record_id=delivery_record_id,
        repo_id=repo_id,
        workspace_root=str(workspace_root) if workspace_root is not None else None,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context=context_payload,
    )


async def _deliver_bound_discord(
    *,
    hub_root: Path,
    raw_config: dict[str, Any],
    workspace_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str,
    run_id: Optional[str],
    managed_thread_id: Optional[str],
    context_payload: Optional[dict[str, Any]],
    notification_store: PmaNotificationStore,
) -> dict[str, Any]:
    from ..integrations.discord.rendering import (
        chunk_discord_message,
        format_discord_message,
    )
    from ..integrations.discord.state import DiscordStateStore
    from ..integrations.discord.state import OutboxRecord as DiscordOutboxRecord

    normalized_repo_id = _normalize_optional_text(repo_id)
    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    created_at = now_iso()
    targets = 0
    published = 0
    store = DiscordStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="discord_bot",
            default_state_file=DISCORD_STATE_FILE_DEFAULT,
        )
    )
    try:
        bindings = await store.list_bindings()
        channels: list[str] = []
        for binding in bindings:
            if bool(binding.get("pma_enabled")):
                continue
            if not _binding_matches_workspace(
                binding.get("workspace_path"), workspace_root
            ):
                continue
            binding_repo_id = _resolve_repo_id(
                repo_id=binding.get("repo_id"),
                workspace_path=binding.get("workspace_path"),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            if normalized_repo_id and binding_repo_id not in {None, normalized_repo_id}:
                continue
            channel_id = _normalize_optional_text(binding.get("channel_id"))
            if channel_id is None or channel_id in channels:
                continue
            channels.append(channel_id)
        if not channels:
            return {"route": "bound", "targets": 0, "published": 0}
        chunks = chunk_discord_message(
            format_discord_message(message),
            max_len=_DISCORD_MESSAGE_MAX_LEN,
            with_numbering=False,
        )
        if not chunks:
            chunks = [format_discord_message(message)]
        for channel_id in channels:
            targets += 1
            for index, chunk in enumerate(chunks, start=1):
                record_id = _build_discord_record_id(
                    correlation_id=correlation_id,
                    delivery_mode="bound",
                    channel_id=channel_id,
                    index=index,
                )
                _record_notification_delivery(
                    notification_store,
                    correlation_id=correlation_id,
                    source_kind=source_kind,
                    delivery_mode="bound",
                    surface_kind="discord",
                    surface_key=channel_id,
                    delivery_record_id=record_id,
                    repo_id=normalized_repo_id,
                    workspace_root=workspace_root,
                    run_id=run_id,
                    managed_thread_id=managed_thread_id,
                    context_payload=context_payload,
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
    finally:
        await store.close()
    return {"route": "bound", "targets": targets, "published": published}


async def _deliver_bound_telegram(
    *,
    hub_root: Path,
    raw_config: dict[str, Any],
    workspace_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str,
    run_id: Optional[str],
    managed_thread_id: Optional[str],
    context_payload: Optional[dict[str, Any]],
    notification_store: PmaNotificationStore,
) -> dict[str, Any]:
    from ..integrations.telegram.state import (
        OutboxRecord as TelegramOutboxRecord,
    )
    from ..integrations.telegram.state import TelegramStateStore, parse_topic_key

    normalized_repo_id = _normalize_optional_text(repo_id)
    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    created_at = now_iso()
    targets = 0
    published = 0
    store = TelegramStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="telegram_bot",
            default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
        )
    )
    try:
        topics = await store.list_topics()
        for surface_key in sorted(topics):
            topic = topics[surface_key]
            if bool(getattr(topic, "pma_enabled", False)):
                continue
            try:
                chat_id, thread_id, scope = parse_topic_key(surface_key)
            except Exception:
                continue
            base_key = f"{chat_id}:{thread_id or 'root'}"
            if scope != await store.get_topic_scope(base_key):
                continue
            if not _binding_matches_workspace(
                getattr(topic, "workspace_path", None), workspace_root
            ):
                continue
            binding_repo_id = _resolve_repo_id(
                repo_id=getattr(topic, "repo_id", None),
                workspace_path=getattr(topic, "workspace_path", None),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            if normalized_repo_id and binding_repo_id not in {None, normalized_repo_id}:
                continue
            record_id = _build_telegram_record_id(
                correlation_id=correlation_id,
                delivery_mode="bound",
                chat_id=chat_id,
                thread_id=thread_id,
            )
            _record_notification_delivery(
                notification_store,
                correlation_id=correlation_id,
                source_kind=source_kind,
                delivery_mode="bound",
                surface_kind="telegram",
                surface_key=surface_key,
                delivery_record_id=record_id,
                repo_id=normalized_repo_id,
                workspace_root=workspace_root,
                run_id=run_id,
                managed_thread_id=managed_thread_id,
                context_payload=context_payload,
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
                    text=message,
                    created_at=created_at,
                    operation="send",
                    message_id=None,
                    outbox_key=f"pma-notice:{correlation_id}:{surface_key}:send",
                )
            )
            targets += 1
            published += 1
    finally:
        await store.close()
    return {"route": "bound", "targets": targets, "published": published}


async def _deliver_primary_pma_discord(
    *,
    hub_root: Path,
    raw_config: dict[str, Any],
    repo_id: str,
    message: str,
    correlation_id: str,
    source_kind: str,
    run_id: Optional[str],
    managed_thread_id: Optional[str],
    context_payload: Optional[dict[str, Any]],
    notification_store: PmaNotificationStore,
) -> dict[str, Any]:
    from ..integrations.discord.rendering import (
        chunk_discord_message,
        format_discord_message,
    )
    from ..integrations.discord.state import DiscordStateStore
    from ..integrations.discord.state import OutboxRecord as DiscordOutboxRecord

    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    created_at = now_iso()
    candidates: list[tuple[str, str, Optional[Path]]] = []
    store = DiscordStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="discord_bot",
            default_state_file=DISCORD_STATE_FILE_DEFAULT,
        )
    )
    try:
        for binding in await store.list_bindings():
            if not bool(binding.get("pma_enabled")):
                continue
            binding_repo_id = _resolve_repo_id(
                repo_id=binding.get("repo_id"),
                workspace_path=binding.get("workspace_path"),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            prev_binding_repo_id = _resolve_repo_id(
                repo_id=binding.get("pma_prev_repo_id"),
                workspace_path=binding.get("pma_prev_workspace_path"),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            prev_repo_id = _normalize_optional_text(binding.get("pma_prev_repo_id"))
            if repo_id not in {binding_repo_id, prev_repo_id, prev_binding_repo_id}:
                continue
            channel_id = _normalize_optional_text(binding.get("channel_id"))
            updated_at = _normalize_optional_text(binding.get("updated_at")) or ""
            workspace_path = _normalize_optional_text(
                binding.get("pma_prev_workspace_path") or binding.get("workspace_path")
            )
            if channel_id is not None:
                candidates.append(
                    (
                        updated_at,
                        channel_id,
                        Path(workspace_path) if workspace_path else None,
                    )
                )
        if not candidates:
            return {"route": "primary_pma", "targets": 0, "published": 0}
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _updated_at, channel_id, candidate_workspace_root = candidates[0]
        chunks = chunk_discord_message(
            format_discord_message(message),
            max_len=_DISCORD_MESSAGE_MAX_LEN,
            with_numbering=False,
        )
        if not chunks:
            chunks = [format_discord_message(message)]
        published = 0
        for index, chunk in enumerate(chunks, start=1):
            record_id = _build_discord_record_id(
                correlation_id=correlation_id,
                delivery_mode="primary_pma",
                channel_id=channel_id,
                index=index,
            )
            _record_notification_delivery(
                notification_store,
                correlation_id=correlation_id,
                source_kind=source_kind,
                delivery_mode="primary_pma",
                surface_kind="discord",
                surface_key=channel_id,
                delivery_record_id=record_id,
                repo_id=repo_id,
                workspace_root=candidate_workspace_root,
                run_id=run_id,
                managed_thread_id=managed_thread_id,
                context_payload=context_payload,
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
        return {"route": "primary_pma", "targets": 1, "published": published}
    finally:
        await store.close()


async def _deliver_primary_pma_telegram(
    *,
    hub_root: Path,
    raw_config: dict[str, Any],
    repo_id: str,
    message: str,
    correlation_id: str,
    source_kind: str,
    run_id: Optional[str],
    managed_thread_id: Optional[str],
    context_payload: Optional[dict[str, Any]],
    notification_store: PmaNotificationStore,
) -> dict[str, Any]:
    from ..integrations.telegram.state import (
        OutboxRecord as TelegramOutboxRecord,
    )
    from ..integrations.telegram.state import TelegramStateStore, parse_topic_key

    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    created_at = now_iso()
    store = TelegramStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="telegram_bot",
            default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
        )
    )
    try:
        topics = await store.list_topics()
        for surface_key in sorted(topics):
            topic = topics[surface_key]
            if not bool(getattr(topic, "pma_enabled", False)):
                continue
            try:
                chat_id, thread_id, scope = parse_topic_key(surface_key)
            except Exception:
                continue
            base_key = f"{chat_id}:{thread_id or 'root'}"
            if scope != await store.get_topic_scope(base_key):
                continue
            binding_repo_id = _resolve_repo_id(
                repo_id=getattr(topic, "repo_id", None),
                workspace_path=getattr(topic, "workspace_path", None),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            prev_binding_repo_id = _resolve_repo_id(
                repo_id=getattr(topic, "pma_prev_repo_id", None),
                workspace_path=getattr(topic, "pma_prev_workspace_path", None),
                repo_id_by_workspace=repo_id_by_workspace,
            )
            prev_repo_id = _normalize_optional_text(
                getattr(topic, "pma_prev_repo_id", None)
            )
            if repo_id not in {binding_repo_id, prev_repo_id, prev_binding_repo_id}:
                continue
            record_id = _build_telegram_record_id(
                correlation_id=correlation_id,
                delivery_mode="primary_pma",
                chat_id=chat_id,
                thread_id=thread_id,
            )
            workspace_path_raw = _normalize_optional_text(
                getattr(topic, "pma_prev_workspace_path", None)
                or getattr(topic, "workspace_path", None)
            )
            _record_notification_delivery(
                notification_store,
                correlation_id=correlation_id,
                source_kind=source_kind,
                delivery_mode="primary_pma",
                surface_kind="telegram",
                surface_key=surface_key,
                delivery_record_id=record_id,
                repo_id=repo_id,
                workspace_root=Path(workspace_path_raw) if workspace_path_raw else None,
                run_id=run_id,
                managed_thread_id=managed_thread_id,
                context_payload=context_payload,
            )
            if await store.get_outbox(record_id) is not None:
                return {"route": "primary_pma", "targets": 1, "published": 0}
            await store.enqueue_outbox(
                TelegramOutboxRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=None,
                    placeholder_message_id=None,
                    text=message,
                    created_at=created_at,
                    operation="send",
                    message_id=None,
                    outbox_key=f"pma-escalation:{correlation_id}:{surface_key}:send",
                )
            )
            return {"route": "primary_pma", "targets": 1, "published": 1}
    finally:
        await store.close()
    return {"route": "primary_pma", "targets": 0, "published": 0}


async def deliver_pma_notification(
    *,
    hub_root: Path,
    message: str,
    correlation_id: str,
    delivery: str,
    source_kind: str,
    repo_id: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    context_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    text = str(message or "").strip()
    normalized_repo_id = _normalize_optional_text(repo_id)
    normalized_source_kind = _normalize_optional_text(source_kind) or "automation"
    normalized_delivery = (_normalize_optional_text(delivery) or "auto").lower()
    if not text:
        return {"route": normalized_delivery, "targets": 0, "published": 0}
    try:
        raw_config = load_hub_config(hub_root).raw
    except Exception:
        raw_config = {}
    notification_store = PmaNotificationStore(hub_root)
    payload = _notification_context_payload(
        message=text,
        correlation_id=correlation_id,
        source_kind=normalized_source_kind,
        context_payload=context_payload,
    )
    if normalized_delivery == "none":
        return {"route": "none", "targets": 0, "published": 0}
    if normalized_delivery in {"auto", "primary_pma"} and normalized_repo_id:
        pma_discord = await _deliver_primary_pma_discord(
            hub_root=hub_root,
            raw_config=raw_config,
            repo_id=normalized_repo_id,
            message=text,
            correlation_id=correlation_id,
            source_kind=normalized_source_kind,
            run_id=run_id,
            managed_thread_id=managed_thread_id,
            context_payload=payload,
            notification_store=notification_store,
        )
        if pma_discord.get("targets", 0) > 0:
            return pma_discord
        pma_telegram = await _deliver_primary_pma_telegram(
            hub_root=hub_root,
            raw_config=raw_config,
            repo_id=normalized_repo_id,
            message=text,
            correlation_id=correlation_id,
            source_kind=normalized_source_kind,
            run_id=run_id,
            managed_thread_id=managed_thread_id,
            context_payload=payload,
            notification_store=notification_store,
        )
        if pma_telegram.get("targets", 0) > 0:
            return pma_telegram
        if normalized_delivery == "primary_pma":
            return pma_telegram
    if normalized_delivery in {"auto", "bound"} and workspace_root is not None:
        preferred_source = preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=hub_root,
            raw_config=raw_config,
            workspace_root=workspace_root,
        )
        ordered_sources = (
            [preferred_source]
            + [
                source
                for source in ("discord", "telegram")
                if source != preferred_source
            ]
            if preferred_source in {"discord", "telegram"}
            else ["discord", "telegram"]
        )
        last_outcome = {"route": "bound", "targets": 0, "published": 0}
        for source in ordered_sources:
            if source == "discord":
                outcome = await _deliver_bound_discord(
                    hub_root=hub_root,
                    raw_config=raw_config,
                    workspace_root=workspace_root,
                    repo_id=normalized_repo_id,
                    message=text,
                    correlation_id=correlation_id,
                    source_kind=normalized_source_kind,
                    run_id=run_id,
                    managed_thread_id=managed_thread_id,
                    context_payload=payload,
                    notification_store=notification_store,
                )
            else:
                outcome = await _deliver_bound_telegram(
                    hub_root=hub_root,
                    raw_config=raw_config,
                    workspace_root=workspace_root,
                    repo_id=normalized_repo_id,
                    message=text,
                    correlation_id=correlation_id,
                    source_kind=normalized_source_kind,
                    run_id=run_id,
                    managed_thread_id=managed_thread_id,
                    context_payload=payload,
                    notification_store=notification_store,
                )
            if outcome.get("targets", 0) > 0:
                return outcome
            last_outcome = outcome
        return last_outcome
    return {"route": normalized_delivery, "targets": 0, "published": 0}


async def notify_preferred_bound_chat_for_workspace(
    *,
    hub_root: Path,
    workspace_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str = "notice",
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    context_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return await deliver_pma_notification(
        hub_root=hub_root,
        workspace_root=workspace_root,
        repo_id=repo_id,
        message=message,
        correlation_id=correlation_id,
        delivery="bound",
        source_kind=source_kind,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context_payload=context_payload,
    )


async def notify_primary_pma_chat_for_repo(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
    source_kind: str = "notice",
    workspace_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    context_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return await deliver_pma_notification(
        hub_root=hub_root,
        workspace_root=workspace_root,
        repo_id=repo_id,
        message=message,
        correlation_id=correlation_id,
        delivery="primary_pma",
        source_kind=source_kind,
        run_id=run_id,
        managed_thread_id=managed_thread_id,
        context_payload=context_payload,
    )


__all__ = [
    "deliver_pma_notification",
    "notify_preferred_bound_chat_for_workspace",
    "notify_primary_pma_chat_for_repo",
]
