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


async def notify_preferred_bound_chat_for_workspace(
    *,
    hub_root: Path,
    workspace_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
) -> dict[str, int]:
    text = str(message or "").strip()
    if not text:
        return {"targets": 0, "published": 0}

    try:
        raw_config = load_hub_config(hub_root).raw
    except Exception:
        raw_config = {}
    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)
    preferred_source = preferred_non_pma_chat_notification_source_for_workspace(
        hub_root=hub_root,
        raw_config=raw_config,
        workspace_root=workspace_root,
    )
    if preferred_source not in {"discord", "telegram"}:
        return {"targets": 0, "published": 0}

    created_at = now_iso()
    normalized_repo_id = _normalize_optional_text(repo_id)

    async def _notify_discord() -> dict[str, int]:
        from ..integrations.discord.rendering import (
            chunk_discord_message,
            format_discord_message,
        )
        from ..integrations.discord.state import DiscordStateStore
        from ..integrations.discord.state import OutboxRecord as DiscordOutboxRecord

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
                return {"targets": 0, "published": 0}
            chunks = chunk_discord_message(
                format_discord_message(text),
                max_len=_DISCORD_MESSAGE_MAX_LEN,
                with_numbering=False,
            )
            if not chunks:
                chunks = [format_discord_message(text)]
            for channel_id in channels:
                targets += 1
                for index, chunk in enumerate(chunks, start=1):
                    digest = hashlib.sha256(
                        f"{correlation_id}:bound:{channel_id}:{index}".encode("utf-8")
                    ).hexdigest()[:24]
                    record_id = f"pma-notice:{digest}"
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
        return {"targets": targets, "published": published}

    async def _notify_telegram() -> dict[str, int]:
        from ..integrations.telegram.state import (
            OutboxRecord as TelegramOutboxRecord,
        )
        from ..integrations.telegram.state import TelegramStateStore, parse_topic_key

        targets = 0
        published = 0
        telegram_store = TelegramStateStore(
            _resolve_state_path(
                hub_root,
                raw_config,
                section="telegram_bot",
                default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
            )
        )
        try:
            topics = await telegram_store.list_topics()
            for surface_key in sorted(topics):
                topic = topics[surface_key]
                if bool(getattr(topic, "pma_enabled", False)):
                    continue
                try:
                    chat_id, thread_id, scope = parse_topic_key(surface_key)
                except Exception:
                    continue
                base_key = f"{chat_id}:{thread_id or 'root'}"
                current_scope = await telegram_store.get_topic_scope(base_key)
                if scope != current_scope:
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
                if normalized_repo_id and binding_repo_id not in {
                    None,
                    normalized_repo_id,
                }:
                    continue
                digest = hashlib.sha256(
                    f"{correlation_id}:bound:{chat_id}:{thread_id or 'root'}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24]
                record_id = f"pma-notice:{digest}"
                if await telegram_store.get_outbox(record_id) is not None:
                    continue
                await telegram_store.enqueue_outbox(
                    TelegramOutboxRecord(
                        record_id=record_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        reply_to_message_id=None,
                        placeholder_message_id=None,
                        text=text,
                        created_at=created_at,
                        operation="send",
                        message_id=None,
                        outbox_key=f"pma-notice:{correlation_id}:{surface_key}:send",
                    )
                )
                targets += 1
                published += 1
        finally:
            await telegram_store.close()
        return {"targets": targets, "published": published}

    notify_by_source = {"discord": _notify_discord, "telegram": _notify_telegram}
    ordered_sources = [preferred_source] + [
        source for source in ("discord", "telegram") if source != preferred_source
    ]
    last_outcome = {"targets": 0, "published": 0}
    for source in ordered_sources:
        outcome = await notify_by_source[source]()
        if outcome.get("targets", 0) > 0:
            return outcome
        last_outcome = outcome
    return last_outcome


async def notify_primary_pma_chat_for_repo(
    *,
    hub_root: Path,
    repo_id: Optional[str],
    message: str,
    correlation_id: str,
) -> dict[str, int]:
    text = str(message or "").strip()
    normalized_repo_id = _normalize_optional_text(repo_id)
    if not text or normalized_repo_id is None:
        return {"targets": 0, "published": 0}

    try:
        raw_config = load_hub_config(hub_root).raw
    except Exception:
        raw_config = {}
    repo_id_by_workspace = _repo_id_by_workspace_path(hub_root)

    created_at = now_iso()
    discord_candidates: list[tuple[str, str]] = []
    from ..integrations.discord.rendering import (
        chunk_discord_message,
        format_discord_message,
    )
    from ..integrations.discord.state import DiscordStateStore
    from ..integrations.discord.state import OutboxRecord as DiscordOutboxRecord

    discord_store = DiscordStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="discord_bot",
            default_state_file=DISCORD_STATE_FILE_DEFAULT,
        )
    )
    try:
        for binding in await discord_store.list_bindings():
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
            if normalized_repo_id not in {
                binding_repo_id,
                prev_repo_id,
                prev_binding_repo_id,
            }:
                continue
            channel_id = _normalize_optional_text(binding.get("channel_id"))
            updated_at = _normalize_optional_text(binding.get("updated_at")) or ""
            if channel_id is not None:
                discord_candidates.append((updated_at, channel_id))

        if discord_candidates:
            discord_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            channel_id = discord_candidates[0][1]
            chunks = chunk_discord_message(
                format_discord_message(text),
                max_len=_DISCORD_MESSAGE_MAX_LEN,
                with_numbering=False,
            )
            if not chunks:
                chunks = [format_discord_message(text)]
            published = 0
            for index, chunk in enumerate(chunks, start=1):
                digest = hashlib.sha256(
                    f"{correlation_id}:pma:{channel_id}:{index}".encode("utf-8")
                ).hexdigest()[:24]
                record_id = f"pma-escalation:{digest}"
                if await discord_store.get_outbox(record_id) is not None:
                    continue
                await discord_store.enqueue_outbox(
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
            return {"targets": 1, "published": published}
    finally:
        await discord_store.close()

    from ..integrations.telegram.state import (
        OutboxRecord as TelegramOutboxRecord,
    )
    from ..integrations.telegram.state import TelegramStateStore, parse_topic_key

    telegram_store = TelegramStateStore(
        _resolve_state_path(
            hub_root,
            raw_config,
            section="telegram_bot",
            default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
        )
    )
    try:
        topics = await telegram_store.list_topics()
        for surface_key in sorted(topics):
            topic = topics[surface_key]
            if not bool(getattr(topic, "pma_enabled", False)):
                continue
            try:
                chat_id, thread_id, scope = parse_topic_key(surface_key)
            except Exception:
                continue
            base_key = f"{chat_id}:{thread_id or 'root'}"
            current_scope = await telegram_store.get_topic_scope(base_key)
            if scope != current_scope:
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
            if normalized_repo_id not in {
                binding_repo_id,
                prev_repo_id,
                prev_binding_repo_id,
            }:
                continue
            digest = hashlib.sha256(
                f"{correlation_id}:pma:{chat_id}:{thread_id or 'root'}".encode("utf-8")
            ).hexdigest()[:24]
            record_id = f"pma-escalation:{digest}"
            if await telegram_store.get_outbox(record_id) is not None:
                return {"targets": 1, "published": 0}
            await telegram_store.enqueue_outbox(
                TelegramOutboxRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=None,
                    placeholder_message_id=None,
                    text=text,
                    created_at=created_at,
                    operation="send",
                    message_id=None,
                    outbox_key=f"pma-escalation:{correlation_id}:{surface_key}:send",
                )
            )
            return {"targets": 1, "published": 1}
    finally:
        await telegram_store.close()
    return {"targets": 0, "published": 0}


__all__ = [
    "notify_preferred_bound_chat_for_workspace",
    "notify_primary_pma_chat_for_repo",
]
