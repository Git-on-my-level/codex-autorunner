from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .....core.chat_bindings import (
    DISCORD_STATE_FILE_DEFAULT,
    TELEGRAM_STATE_FILE_DEFAULT,
)
from .....core.logging_utils import log_event
from .....core.time_utils import now_iso
from .....integrations.discord.rendering import (
    chunk_discord_message,
    format_discord_message,
)
from .....integrations.discord.state import (
    DiscordStateStore,
)
from .....integrations.discord.state import (
    OutboxRecord as DiscordOutboxRecord,
)
from .....integrations.telegram.state import (
    OutboxRecord as TelegramOutboxRecord,
)
from .....integrations.telegram.state import (
    TelegramStateStore,
    parse_topic_key,
    topic_key,
)
from .....manifest import load_manifest

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

PMA_PUBLISH_RETRY_DELAYS_SECONDS = (0.0, 0.25, 0.75)
PMA_DISCORD_MESSAGE_MAX_LEN = 1900


def normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = value if isinstance(value, str) else str(value)
    return normalized.strip() or None


def normalize_workspace_path_for_repo_lookup(
    value: Any, *, hub_root: Path
) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = hub_root / candidate
    try:
        return str(candidate.resolve())
    except Exception:
        return str(candidate.absolute())


def resolve_chat_state_path(
    request: Request, *, section: str, default_state_file: str
) -> Path:
    hub_root = request.app.state.config.root
    raw = getattr(request.app.state.config, "raw", {})
    section_cfg = raw.get(section) if isinstance(raw, dict) else {}
    if not isinstance(section_cfg, dict):
        section_cfg = {}
    state_file = section_cfg.get("state_file")
    if not isinstance(state_file, str) or not state_file.strip():
        state_file = default_state_file
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = (hub_root / state_path).resolve()
    return state_path


def repo_id_by_workspace_path(request: Request) -> dict[str, str]:
    hub_root = request.app.state.config.root
    manifest_path = request.app.state.config.manifest_path
    if not manifest_path.exists():
        return {}
    try:
        manifest = load_manifest(manifest_path, hub_root)
    except Exception:
        logger.exception("Failed loading manifest for PMA publish target lookup")
        return {}
    mapping: dict[str, str] = {}
    for repo in manifest.repos:
        try:
            workspace_path = (hub_root / repo.path).resolve()
        except Exception:
            continue
        mapping[str(workspace_path)] = repo.id
    return mapping


def resolve_binding_repo_id(
    *,
    repo_id: Any,
    workspace_path: Any,
    repo_id_by_workspace: dict[str, str],
    hub_root: Path,
) -> Optional[str]:
    normalized_repo_id = normalize_optional_text(repo_id)
    if normalized_repo_id:
        return normalized_repo_id
    normalized_workspace = normalize_workspace_path_for_repo_lookup(
        workspace_path, hub_root=hub_root
    )
    if normalized_workspace:
        mapped_repo = repo_id_by_workspace.get(normalized_workspace)
        if isinstance(mapped_repo, str) and mapped_repo.strip():
            return mapped_repo.strip()
    return None


def resolve_publish_repo_id(
    *,
    request: Request,
    lifecycle_event: Optional[dict[str, Any]],
    wake_up: Optional[dict[str, Any]],
) -> Optional[str]:
    from .....core.pma_thread_store import PmaThreadStore

    for candidate in (
        lifecycle_event.get("repo_id") if lifecycle_event else None,
        wake_up.get("repo_id") if wake_up else None,
    ):
        normalized = normalize_optional_text(candidate)
        if normalized:
            return normalized

    thread_id = (
        normalize_optional_text(wake_up.get("thread_id"))
        if isinstance(wake_up, dict)
        else None
    )
    if not thread_id:
        return None
    try:
        thread = PmaThreadStore(request.app.state.config.root).get_thread(thread_id)
    except Exception:
        logger.exception(
            "Failed resolving managed thread repo for publish thread_id=%s",
            thread_id,
        )
        return None
    if not isinstance(thread, dict):
        return None
    return normalize_optional_text(thread.get("repo_id"))


def build_publish_correlation_id(
    *,
    result: dict[str, Any],
    client_turn_id: Optional[str],
    wake_up: Optional[dict[str, Any]],
) -> str:
    for candidate in (
        client_turn_id,
        result.get("client_turn_id"),
        result.get("turn_id"),
        wake_up.get("wakeup_id") if isinstance(wake_up, dict) else None,
    ):
        normalized = normalize_optional_text(candidate)
        if normalized:
            return normalized
    return f"pma-{uuid.uuid4().hex[:12]}"


def build_publish_message(
    *,
    result: dict[str, Any],
    lifecycle_event: Optional[dict[str, Any]],
    wake_up: Optional[dict[str, Any]],
    correlation_id: str,
) -> str:
    trigger = (
        normalize_optional_text(lifecycle_event.get("event_type"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not trigger and isinstance(wake_up, dict):
        trigger = normalize_optional_text(wake_up.get("event_type")) or (
            normalize_optional_text(wake_up.get("source")) or "automation"
        )
    trigger = trigger or "automation"

    repo_id = (
        normalize_optional_text(lifecycle_event.get("repo_id"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not repo_id and isinstance(wake_up, dict):
        repo_id = normalize_optional_text(wake_up.get("repo_id"))

    run_id = (
        normalize_optional_text(lifecycle_event.get("run_id"))
        if isinstance(lifecycle_event, dict)
        else None
    )
    if not run_id and isinstance(wake_up, dict):
        run_id = normalize_optional_text(wake_up.get("run_id"))

    thread_id = (
        normalize_optional_text(wake_up.get("thread_id"))
        if isinstance(wake_up, dict)
        else None
    )
    status = normalize_optional_text(result.get("status")) or "error"
    detail = normalize_optional_text(result.get("detail"))
    output = normalize_optional_text(result.get("message"))

    lines: list[str] = [f"PMA update ({trigger})"]
    if repo_id:
        lines.append(f"repo_id: {repo_id}")
    if run_id:
        lines.append(f"run_id: {run_id}")
    if thread_id:
        lines.append(f"thread_id: {thread_id}")
    lines.append(f"correlation_id: {correlation_id}")
    lines.append("")

    if status == "ok":
        lines.append(output or "Turn completed with no assistant output.")
    else:
        lines.append(f"status: {status}")
        lines.append(f"error: {detail or 'Turn failed without detail.'}")
        lines.append("next_action: run /pma status and inspect PMA history if needed.")
    return "\n".join(lines).strip()


def delivery_status_from_counts(
    *,
    published: int,
    duplicates: int,
    failed: int,
    targets: int,
) -> str:
    if targets <= 0:
        return "skipped"
    if published > 0 and failed <= 0:
        return "success"
    if published > 0 and failed > 0:
        return "partial_success"
    if published <= 0 and failed <= 0 and duplicates > 0:
        return "duplicate_only"
    if failed > 0:
        return "failed"
    return "skipped"


async def enqueue_with_retry(enqueue_call: Any) -> None:
    last_error: Optional[Exception] = None
    for delay_seconds in PMA_PUBLISH_RETRY_DELAYS_SECONDS:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        try:
            await enqueue_call()
            return
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


async def publish_automation_result(
    *,
    request: Request,
    result: dict[str, Any],
    client_turn_id: Any,
    lifecycle_event: Any,
    wake_up: Any,
) -> dict[str, Any]:
    hub_root = request.app.state.config.root

    lifecycle_event_dict = (
        lifecycle_event if isinstance(lifecycle_event, dict) else None
    )
    wake_up_dict = wake_up if isinstance(wake_up, dict) else None
    client_turn_id_str = normalize_optional_text(client_turn_id)

    correlation_id = build_publish_correlation_id(
        result=result,
        client_turn_id=client_turn_id_str,
        wake_up=wake_up_dict,
    )
    target_repo_id = resolve_publish_repo_id(
        request=request,
        lifecycle_event=lifecycle_event_dict,
        wake_up=wake_up_dict,
    )
    repo_id_by_workspace = repo_id_by_workspace_path(request)
    message = build_publish_message(
        result=result,
        lifecycle_event=lifecycle_event_dict,
        wake_up=wake_up_dict,
        correlation_id=correlation_id,
    )

    published = 0
    duplicates = 0
    failed = 0
    targets = 0
    errors: list[str] = []

    discord_store: Optional[DiscordStateStore] = None
    telegram_store: Optional[TelegramStateStore] = None
    try:
        discord_state_path = resolve_chat_state_path(
            request,
            section="discord_bot",
            default_state_file=DISCORD_STATE_FILE_DEFAULT,
        )
        if discord_state_path.exists():
            discord_store = DiscordStateStore(discord_state_path)
            bindings = await discord_store.list_bindings()
            seen_channels: set[str] = set()
            for binding in bindings:
                if not bool(binding.get("pma_enabled")):
                    continue
                channel_id = normalize_optional_text(binding.get("channel_id"))
                if not channel_id or channel_id in seen_channels:
                    continue
                binding_repo_id = resolve_binding_repo_id(
                    repo_id=binding.get("repo_id"),
                    workspace_path=binding.get("workspace_path"),
                    repo_id_by_workspace=repo_id_by_workspace,
                    hub_root=hub_root,
                )
                prev_repo_id = normalize_optional_text(binding.get("pma_prev_repo_id"))
                if target_repo_id and target_repo_id not in {
                    binding_repo_id,
                    prev_repo_id,
                }:
                    continue
                seen_channels.add(channel_id)
                targets += 1
                chunks = chunk_discord_message(
                    format_discord_message(message),
                    max_len=PMA_DISCORD_MESSAGE_MAX_LEN,
                    with_numbering=False,
                )
                if not chunks:
                    chunks = [format_discord_message(message)]
                for index, chunk in enumerate(chunks, start=1):
                    digest = hashlib.sha256(
                        f"{correlation_id}:discord:{channel_id}:{index}".encode("utf-8")
                    ).hexdigest()[:24]
                    record_id = f"pma:{digest}"
                    existing = await discord_store.get_outbox(record_id)
                    if existing is not None:
                        duplicates += 1
                        continue
                    record = DiscordOutboxRecord(
                        record_id=record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json={"content": chunk},
                        created_at=now_iso(),
                    )
                    try:
                        await enqueue_with_retry(
                            lambda record=record: discord_store.enqueue_outbox(record)
                        )
                    except Exception as exc:
                        failed += 1
                        errors.append(f"discord:{channel_id}:{exc}")
                        logger.warning(
                            "Failed to enqueue PMA discord publish: channel_id=%s correlation_id=%s error=%s",
                            channel_id,
                            correlation_id,
                            exc,
                        )
                    else:
                        published += 1

        telegram_state_path = resolve_chat_state_path(
            request,
            section="telegram_bot",
            default_state_file=TELEGRAM_STATE_FILE_DEFAULT,
        )
        if telegram_state_path.exists():
            telegram_store = TelegramStateStore(telegram_state_path)
            topics = await telegram_store.list_topics()
            seen_topics: set[tuple[int, Optional[int]]] = set()
            for key, topic in topics.items():
                if not bool(getattr(topic, "pma_enabled", False)):
                    continue
                try:
                    chat_id, thread_id, scope = parse_topic_key(key)
                except Exception:
                    continue
                base_key = topic_key(chat_id, thread_id)
                current_scope = await telegram_store.get_topic_scope(base_key)
                if scope != current_scope:
                    continue
                identity = (chat_id, thread_id)
                if identity in seen_topics:
                    continue
                binding_repo_id = resolve_binding_repo_id(
                    repo_id=getattr(topic, "repo_id", None),
                    workspace_path=getattr(topic, "workspace_path", None),
                    repo_id_by_workspace=repo_id_by_workspace,
                    hub_root=hub_root,
                )
                prev_repo_id = normalize_optional_text(
                    getattr(topic, "pma_prev_repo_id", None)
                )
                if target_repo_id and target_repo_id not in {
                    binding_repo_id,
                    prev_repo_id,
                }:
                    continue
                seen_topics.add(identity)
                targets += 1
                digest = hashlib.sha256(
                    f"{correlation_id}:telegram:{chat_id}:{thread_id or 'root'}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24]
                record_id = f"pma:{digest}"
                outbox_key = (
                    f"pma:{correlation_id}:{chat_id}:{thread_id or 'root'}:send"
                )
                existing = await telegram_store.get_outbox(record_id)
                if existing is not None:
                    duplicates += 1
                    continue
                record = TelegramOutboxRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    reply_to_message_id=None,
                    placeholder_message_id=None,
                    text=message,
                    created_at=now_iso(),
                    operation="send",
                    message_id=None,
                    outbox_key=outbox_key,
                )
                try:
                    await enqueue_with_retry(
                        lambda record=record: telegram_store.enqueue_outbox(record)
                    )
                except Exception as exc:
                    failed += 1
                    errors.append(f"telegram:{chat_id}:{thread_id or 'root'}:{exc}")
                    logger.warning(
                        "Failed to enqueue PMA telegram publish: chat_id=%s thread_id=%s correlation_id=%s error=%s",
                        chat_id,
                        thread_id,
                        correlation_id,
                        exc,
                    )
                else:
                    published += 1
    finally:
        if discord_store is not None:
            try:
                await discord_store.close()
            except Exception:
                logger.exception("Failed to close discord publish store")
        if telegram_store is not None:
            try:
                await telegram_store.close()
            except Exception:
                logger.exception("Failed to close telegram publish store")

    delivery_status = delivery_status_from_counts(
        published=published,
        duplicates=duplicates,
        failed=failed,
        targets=targets,
    )
    delivery_outcome = {
        "published": published,
        "duplicates": duplicates,
        "failed": failed,
        "targets": targets,
        "repo_id": target_repo_id,
        "correlation_id": correlation_id,
        "errors": errors[:5],
    }
    log_event(
        logger,
        logging.INFO,
        "pma.turn.publish",
        delivery_status=delivery_status,
        targets=targets,
        published=published,
        duplicates=duplicates,
        failed=failed,
        repo_id=target_repo_id,
        correlation_id=correlation_id,
    )
    return {
        "delivery_status": delivery_status,
        "delivery_outcome": delivery_outcome,
    }
