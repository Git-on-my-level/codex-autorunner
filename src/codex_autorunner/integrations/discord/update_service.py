from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ...core.config import ConfigError, load_hub_config
from ...core.update import (
    UpdateInProgressError,
    _available_update_target_definitions,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _update_target_restarts_surface,
)
from ...core.update_paths import resolve_update_paths
from ..chat.update_notifier import (
    ChatUpdateStatusNotifier,
    mark_update_status_notified,
)
from .service_normalization import (
    DiscordMessagePayload,
    DiscordUpdateNoticeContext,
    format_discord_update_status_message,
)

__all__ = [
    "UpdateInProgressError",
    "ChatUpdateStatusNotifier",
    "mark_update_status_notified",
    "_available_update_target_definitions",
    "_format_update_confirmation_warning",
    "_normalize_update_ref",
    "_normalize_update_target",
    "_read_update_status",
    "_spawn_update_process",
    "_update_target_restarts_surface",
    "update_thread_blocks_restart_warning",
    "active_update_session_count",
    "build_update_confirmation_components",
    "update_status_path",
    "format_update_status_message",
    "dynamic_update_target_definitions",
    "send_update_status_notice",
    "mark_notified",
]


def update_thread_blocks_restart_warning(thread: Any) -> bool:
    thread_kind = str(getattr(thread, "thread_kind", "") or "").strip().lower()
    return thread_kind != "ticket_flow"


def active_update_session_count(service: Any) -> int:
    try:
        orchestration_service = service._discord_thread_service()
        threads = orchestration_service.list_thread_targets(lifecycle_status="active")
    except (AttributeError, TypeError, RuntimeError):
        return 0
    get_running_execution = getattr(
        orchestration_service, "get_running_execution", None
    )
    if not callable(get_running_execution):
        return sum(
            1
            for thread in threads
            if update_thread_blocks_restart_warning(thread)
            if str(getattr(thread, "status", "") or "").strip().lower() == "running"
        )

    active_count = 0
    for thread in threads:
        if not update_thread_blocks_restart_warning(thread):
            continue
        thread_target_id = str(getattr(thread, "thread_target_id", "") or "").strip()
        if not thread_target_id:
            continue
        try:
            if get_running_execution(thread_target_id) is not None:
                active_count += 1
        except (AttributeError, TypeError, RuntimeError):
            if str(getattr(thread, "status", "") or "").strip().lower() == "running":
                active_count += 1
    return active_count


def build_update_confirmation_components(
    service: Any,
    *,
    update_target: str,
) -> list[dict[str, Any]]:
    from .components import (
        DISCORD_BUTTON_STYLE_DANGER,
        build_action_row,
        build_button,
    )
    from .interaction_registry import (
        UPDATE_CANCEL_PREFIX,
        UPDATE_CONFIRM_PREFIX,
    )

    return [
        build_action_row(
            [
                build_button(
                    "Update anyway",
                    f"{UPDATE_CONFIRM_PREFIX}:{update_target}",
                    style=DISCORD_BUTTON_STYLE_DANGER,
                ),
                build_button("Cancel", f"{UPDATE_CANCEL_PREFIX}:{update_target}"),
            ]
        )
    ]


def update_status_path(service: Any) -> Path:
    return resolve_update_paths().status_path


def format_update_status_message(service: Any, status: Optional[dict[str, Any]]) -> str:
    return format_discord_update_status_message(status)


def dynamic_update_target_definitions(service: Any):
    raw_config = service._hub_raw_config_cache
    if raw_config is None:
        try:
            raw_config = load_hub_config(service._config.root).raw
        except (ConfigError, OSError, ValueError, RuntimeError):
            raw_config = {}
        service._hub_raw_config_cache = raw_config
    return _available_update_target_definitions(
        raw_config=raw_config if isinstance(raw_config, dict) else None,
        update_backend=service._update_backend,
        linux_service_names=(
            service._update_linux_service_names
            if isinstance(service._update_linux_service_names, dict)
            else None
        ),
    )


async def send_update_status_notice(
    service: Any, notify_context: dict[str, Any], text: str
) -> None:
    context = DiscordUpdateNoticeContext.from_raw(notify_context)
    if context is None:
        return
    await service._send_channel_message_safe(
        context.chat_id,
        DiscordMessagePayload(content=text).to_payload(),
    )


def mark_notified(service: Any, status: dict[str, Any]) -> None:
    from . import service as service_module

    mark_fn = getattr(service_module, "mark_update_status_notified", None)
    if not callable(mark_fn):
        return
    mark_fn(
        path=update_status_path(service),
        status=status,
        logger=service._logger,
        log_event_name="discord.update.notify_write_failed",
    )
