from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from ....core.config import ConfigError, load_hub_config
from ....core.constants import DEFAULT_UPDATE_REPO_REF, DEFAULT_UPDATE_REPO_URL
from ....core.update import (
    UpdateInProgressError,
    _available_update_target_definitions,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _update_target_restarts_surface,
)
from ....core.update_paths import resolve_update_paths
from ....core.update_targets import get_update_target_label
from ....core.utils import canonicalize_path
from ...chat.constants import TOPIC_NOT_BOUND_DISCORD_MESSAGE
from ..components import (
    DISCORD_BUTTON_STYLE_DANGER,
    build_action_row,
    build_button,
    build_update_target_picker,
)
from ..interaction_registry import (
    UPDATE_CANCEL_PREFIX,
    UPDATE_CONFIRM_PREFIX,
    UPDATE_TARGET_SELECT_ID,
)
from ..interaction_runtime import (
    ensure_component_response_deferred,
    ensure_ephemeral_response_deferred,
    send_runtime_components_ephemeral,
    send_runtime_ephemeral,
    update_runtime_component_message,
)
from ..rendering import (
    format_discord_message,
)
from ..service_normalization import (
    DiscordMessagePayload,
    DiscordUpdateNoticeContext,
    format_discord_update_status_message,
)


def _update_thread_blocks_restart_warning(thread: Any) -> bool:
    thread_kind = str(getattr(thread, "thread_kind", "") or "").strip().lower()
    return thread_kind != "ticket_flow"


def _active_update_session_count(service: Any) -> int:
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
            if _update_thread_blocks_restart_warning(thread)
            if str(getattr(thread, "status", "") or "").strip().lower() == "running"
        )

    active_count = 0
    for thread in threads:
        if not _update_thread_blocks_restart_warning(thread):
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


def _build_update_confirmation_components(
    service: Any,
    *,
    update_target: str,
) -> list[dict[str, Any]]:
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


def _update_status_path(service: Any) -> Path:
    return resolve_update_paths().status_path


def _format_update_status_message(
    service: Any, status: Optional[dict[str, Any]]
) -> str:
    return format_discord_update_status_message(status)


def _dynamic_update_target_definitions(service: Any):
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


async def _send_update_status_notice(
    service: Any, notify_context: dict[str, Any], text: str
) -> None:
    context = DiscordUpdateNoticeContext.from_raw(notify_context)
    if context is None:
        return
    await service._send_channel_message_safe(
        context.chat_id,
        DiscordMessagePayload(content=text).to_payload(),
    )


def _mark_update_notified(service: Any, status: dict[str, Any]) -> None:
    from ..service import mark_update_status_notified

    mark_update_status_notified(
        path=_update_status_path(service),
        status=status,
        logger=service._logger,
        log_event_name="discord.update.notify_write_failed",
    )


async def handle_car_update(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    options: dict[str, Any],
    response_mode: str = "command",
) -> None:
    from ..service import _spawn_update_process as _spawn_update_process
    from ..service import log_event

    component_response = response_mode == "component"
    raw_target = options.get("target")
    confirmed = bool(options.get("confirmed"))
    if not isinstance(raw_target, str) or not raw_target.strip():
        components = [
            build_update_target_picker(
                custom_id=UPDATE_TARGET_SELECT_ID,
                target_definitions=_dynamic_update_target_definitions(service),
            )
        ]
        if component_response:
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                "Select update target:",
                components=components,
            )
        else:
            await send_runtime_components_ephemeral(
                service,
                interaction_id,
                interaction_token,
                "Select update target:",
                components,
            )
        return
    if isinstance(raw_target, str) and raw_target.strip().lower() == "status":
        await handle_car_update_status(
            service,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            component_response=component_response,
        )
        return

    try:
        update_target = _normalize_update_target(
            raw_target if isinstance(raw_target, str) else None
        )
    except ValueError as exc:
        components = [
            build_update_target_picker(
                custom_id=UPDATE_TARGET_SELECT_ID,
                target_definitions=_dynamic_update_target_definitions(service),
            )
        ]
        text = f"{exc} Select update target:"
        if component_response:
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                text,
                components=components,
            )
        else:
            await send_runtime_components_ephemeral(
                service,
                interaction_id,
                interaction_token,
                text,
                components,
            )
        return
    deferred = (
        await ensure_component_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
        if component_response
        else await ensure_ephemeral_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
    )
    if not confirmed and _update_target_restarts_surface(
        update_target, surface="discord"
    ):
        warning = _format_update_confirmation_warning(
            active_count=service._active_update_session_count(),
            singular_label="Codex session",
        )
        if warning:
            warning_text = format_discord_message(warning)
            components = _build_update_confirmation_components(
                service,
                update_target=update_target,
            )
            if component_response:
                await service.send_or_update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=warning_text,
                    components=components,
                )
            else:
                await service.send_or_respond_ephemeral_with_components(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=warning_text,
                    components=components,
                )
            return

    target_label = get_update_target_label(update_target)
    restarts_discord = _update_target_restarts_surface(update_target, surface="discord")
    if restarts_discord:
        text = format_discord_message(
            f"Preparing update ({target_label}). Checking whether the update can start now. "
            "If it does, the selected service(s) will restart shortly and I will post completion status in this channel. "
            "Use `/car update target:status` for progress."
        )
        if component_response:
            await service.send_or_update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        else:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
    repo_url = (service._update_repo_url or DEFAULT_UPDATE_REPO_URL).strip()
    if not repo_url:
        repo_url = DEFAULT_UPDATE_REPO_URL
    repo_ref = _normalize_update_ref(
        service._update_repo_ref or DEFAULT_UPDATE_REPO_REF
    )
    update_dir = resolve_update_paths().cache_dir
    notify_metadata = service._update_status_notifier.build_spawn_metadata(
        chat_id=channel_id
    )

    linux_hub_service_name: Optional[str] = None
    linux_telegram_service_name: Optional[str] = None
    linux_discord_service_name: Optional[str] = None
    update_services = service._update_linux_service_names
    if isinstance(update_services, dict):
        hub_service = update_services.get("hub")
        telegram_service = update_services.get("telegram")
        discord_service = update_services.get("discord")
        if isinstance(hub_service, str) and hub_service.strip():
            linux_hub_service_name = hub_service.strip()
        if isinstance(telegram_service, str) and telegram_service.strip():
            linux_telegram_service_name = telegram_service.strip()
        if isinstance(discord_service, str) and discord_service.strip():
            linux_discord_service_name = discord_service.strip()

    try:
        await asyncio.to_thread(
            _spawn_update_process,
            repo_url=repo_url,
            repo_ref=repo_ref,
            update_dir=update_dir,
            logger=service._logger,
            update_target=update_target,
            skip_checks=bool(service._update_skip_checks),
            update_backend=service._update_backend,
            linux_hub_service_name=linux_hub_service_name,
            linux_telegram_service_name=linux_telegram_service_name,
            linux_discord_service_name=linux_discord_service_name,
            **notify_metadata,
        )
    except UpdateInProgressError as exc:
        text = format_discord_message(
            f"{exc} Use `/car update target:status` for current state."
        )
        if component_response:
            await service.send_or_update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        else:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        return
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.update.failed_start",
            update_target=update_target,
            exc=exc,
        )
        if component_response:
            await service.send_or_update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Update failed to start. Check logs for details.",
            )
        else:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Update failed to start. Check logs for details.",
            )
        return

    if restarts_discord:
        service._update_status_notifier.schedule_watch({"chat_id": channel_id})
        return
    text = format_discord_message(
        f"Update started ({target_label}). The selected service(s) will restart. "
        "I will post completion status in this channel. "
        "Use `/car update target:status` for progress."
    )
    if component_response:
        await service.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
    else:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
    service._update_status_notifier.schedule_watch({"chat_id": channel_id})


async def handle_car_update_status(
    service: Any,
    *,
    interaction_id: str,
    interaction_token: str,
    component_response: bool = False,
) -> None:
    from ..service import _read_update_status as read_status

    if component_response:
        status = await asyncio.to_thread(read_status)
        if not isinstance(status, dict):
            status = None
        text = _format_update_status_message(service, status)
        await update_runtime_component_message(
            service,
            interaction_id,
            interaction_token,
            text,
            components=[],
        )
        return
    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    status = await asyncio.to_thread(read_status)
    if not isinstance(status, dict):
        status = None
    text = _format_update_status_message(service, status)
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def handle_car_logout(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
) -> None:
    from ..service import log_event

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    client = await service._client_for_workspace(str(workspace_root))
    if client is None:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message(TOPIC_NOT_BOUND_DISCORD_MESSAGE),
        )
        return
    try:
        await client.request("account/logout", params=None)
    except (RuntimeError, ConnectionError, OSError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.logout.failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message("Logout failed; check logs for details."),
        )
        return
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text="Logged out.",
    )


async def handle_car_feedback(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
) -> None:
    from ..service import log_event

    reason = options.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    reason = reason.strip()

    if not reason:
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            "Usage: /car admin feedback reason:<description>",
        )
        return

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    client = await service._client_for_workspace(str(workspace_root))
    if client is None:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message(TOPIC_NOT_BOUND_DISCORD_MESSAGE),
        )
        return

    params: dict[str, Any] = {
        "classification": "bug",
        "reason": reason,
        "includeLogs": True,
    }
    if channel_id:
        binding = await service._store.get_binding(channel_id=channel_id)
        if binding:
            active_thread_id = binding.get("active_thread_id")
            if isinstance(active_thread_id, str) and active_thread_id:
                params["threadId"] = active_thread_id

    try:
        result = await client.request("feedback/upload", params)
    except (RuntimeError, ConnectionError, OSError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.feedback.failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "Feedback upload failed; check logs for details."
            ),
        )
        return

    report_id = None
    if isinstance(result, dict):
        report_id = result.get("threadId") or result.get("id")
    message_text = "Feedback sent."
    if isinstance(report_id, str):
        message_text = f"Feedback sent (report {report_id})."
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=message_text,
    )


async def handle_car_mention(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    path_arg = options.get("path", "")
    request_arg = options.get("request", "Please review this file.")

    if not isinstance(path_arg, str) or not path_arg.strip():
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            "Usage: /car mention path:<file> [request:<text>]",
        )
        return

    path = Path(path_arg.strip())
    if not path.is_absolute():
        path = workspace_root / path

    try:
        path = canonicalize_path(path)
    except (OSError, ValueError):
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            f"Could not resolve path: {path_arg}",
        )
        return

    if not path.exists() or not path.is_file():
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            f"File not found: {path}",
        )
        return

    try:
        path.relative_to(workspace_root)
    except ValueError:
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            "File must be within the bound workspace.",
        )
        return

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    max_bytes = 100000
    try:
        data = path.read_bytes()
    except OSError:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="Failed to read file.",
        )
        return

    if len(data) > max_bytes:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"File too large (max {max_bytes} bytes).",
        )
        return

    null_count = data.count(b"\x00")
    if null_count > 0 and null_count > len(data) * 0.1:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="File appears to be binary; refusing to include it.",
        )
        return

    try:
        display_path = str(path.relative_to(workspace_root))
    except ValueError:
        display_path = str(path)

    if not isinstance(request_arg, str) or not request_arg.strip():
        request_arg = "Please review this file."

    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=(
            f"File `{display_path}` ready for mention.\n\n"
            f"To include it in a request, send a message starting with:\n"
            f"```\n"
            f'<file path="{display_path}">\n'
            f"...\n"
            f"</file>\n"
            f"```\n\n"
            f"Your request: {request_arg}"
        ),
    )


def _format_file_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _list_files_in_dir(folder: Path) -> list[tuple[str, int, str]]:
    from ....core.filebox import list_regular_files

    files: list[tuple[str, int, str]] = []
    for path in list_regular_files(folder):
        try:
            stat = path.stat()
            from datetime import datetime, timezone

            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            files.append((path.name, stat.st_size, mtime))
        except OSError:
            continue
    return files


def _delete_files_in_dir(folder: Path) -> int:
    from ....core.filebox import delete_regular_files

    return delete_regular_files(folder)


async def handle_files_inbox(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
) -> None:
    from ....core.filebox import inbox_dir

    inbox = inbox_dir(workspace_root)
    files = _list_files_in_dir(inbox)
    if not files:
        await service.respond_ephemeral(
            interaction_id, interaction_token, "Inbox: (empty)"
        )
        return
    lines = [f"Inbox ({len(files)} file(s)):"]
    for name, size, mtime in files[:20]:
        lines.append(f"- {name} ({_format_file_size(size)}, {mtime})")
    if len(files) > 20:
        lines.append(f"... and {len(files) - 20} more")
    await service.respond_ephemeral(interaction_id, interaction_token, "\n".join(lines))


async def handle_files_outbox(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
) -> None:
    from ....core.filebox import (
        outbox_dir,
        outbox_pending_dir,
        outbox_sent_dir,
    )

    outbox_root = outbox_dir(workspace_root)
    pending = outbox_pending_dir(workspace_root)
    sent = outbox_sent_dir(workspace_root)
    root_files = _list_files_in_dir(outbox_root)
    root_files = [entry for entry in root_files if entry[0] not in {"pending", "sent"}]
    pending_files = _list_files_in_dir(pending)
    sent_files = _list_files_in_dir(sent)
    lines = []
    if root_files:
        lines.append(f"Outbox root ({len(root_files)} file(s)):")
        for name, size, mtime in root_files[:20]:
            lines.append(f"- {name} ({_format_file_size(size)}, {mtime})")
        if len(root_files) > 20:
            lines.append(f"... and {len(root_files) - 20} more")
        lines.append("")
    if pending_files:
        lines.append(f"Outbox pending ({len(pending_files)} file(s)):")
        for name, size, mtime in pending_files[:20]:
            lines.append(f"- {name} ({_format_file_size(size)}, {mtime})")
        if len(pending_files) > 20:
            lines.append(f"... and {len(pending_files) - 20} more")
    else:
        lines.append("Outbox pending: (empty)")
    lines.append("")
    if sent_files:
        lines.append(f"Outbox sent ({len(sent_files)} file(s)):")
        for name, size, mtime in sent_files[:10]:
            lines.append(f"- {name} ({_format_file_size(size)}, {mtime})")
        if len(sent_files) > 10:
            lines.append(f"... and {len(sent_files) - 10} more")
    else:
        lines.append("Outbox sent: (empty)")
    await service.respond_ephemeral(interaction_id, interaction_token, "\n".join(lines))


async def handle_files_clear(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    from ....core.filebox import (
        inbox_dir,
        outbox_dir,
        outbox_pending_dir,
        outbox_sent_dir,
    )

    target = (options.get("target") or "all").lower().strip()
    inbox = inbox_dir(workspace_root)
    outbox_root = outbox_dir(workspace_root)
    pending = outbox_pending_dir(workspace_root)
    sent = outbox_sent_dir(workspace_root)
    deleted = 0
    if target == "inbox":
        deleted = _delete_files_in_dir(inbox)
    elif target == "outbox":
        deleted = _delete_files_in_dir(outbox_root)
        deleted += _delete_files_in_dir(pending)
        deleted += _delete_files_in_dir(sent)
    elif target == "all":
        deleted = _delete_files_in_dir(inbox)
        deleted += _delete_files_in_dir(outbox_root)
        deleted += _delete_files_in_dir(pending)
        deleted += _delete_files_in_dir(sent)
    else:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Invalid target. Use: inbox, outbox, or all",
        )
        return
    await service.respond_ephemeral(
        interaction_id, interaction_token, f"Deleted {deleted} file(s)."
    )
