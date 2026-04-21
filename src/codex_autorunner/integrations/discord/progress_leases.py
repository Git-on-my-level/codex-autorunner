"""Discord turn progress lease management and reconciliation.

Owns: progress lease CRUD, progress reuse requests, supervision tracking,
background task spawning, progress message retirement, lease reconciliation,
and progress run event application.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Optional, cast

from ...core.ports.run_event import TokenUsage
from ..chat.managed_thread_progress_projector import (
    ManagedThreadProgressProjector,
)
from ..chat.turn_metrics import _extract_context_usage_percent
from . import progress_lease_state as _progress_lease_state
from .errors import (
    DiscordPermanentError,
    DiscordTransientError,
    is_unknown_message_error,
)
from .rendering import (
    format_discord_message,
    truncate_for_discord,
)

_claim_discord_reusable_progress_message = (
    _progress_lease_state._claim_discord_reusable_progress_message
)
_DiscordOrchestrationState = _progress_lease_state._DiscordOrchestrationState
_DiscordProgressReuseRequest = _progress_lease_state._DiscordProgressReuseRequest
_DiscordReusableProgressMessage = _progress_lease_state._DiscordReusableProgressMessage
_DiscordTurnExecutionSupervision = (
    _progress_lease_state._DiscordTurnExecutionSupervision
)
_execution_field = _progress_lease_state._execution_field
_get_discord_thread_queue_task_map = (
    _progress_lease_state._get_discord_thread_queue_task_map
)
_peek_discord_progress_reuse_request = (
    _progress_lease_state._peek_discord_progress_reuse_request
)
_progress_task_context = _progress_lease_state._progress_task_context
_stash_discord_reusable_progress_message = (
    _progress_lease_state._stash_discord_reusable_progress_message
)
bind_discord_progress_task_context = (
    _progress_lease_state.bind_discord_progress_task_context
)
clear_discord_turn_progress_reuse = (
    _progress_lease_state.clear_discord_turn_progress_reuse
)
request_discord_turn_progress_reuse = (
    _progress_lease_state.request_discord_turn_progress_reuse
)

_logger = logging.getLogger(__name__)

_DISCORD_PROGRESS_LIVE_STATES = frozenset({"pending", "active"})
_DISCORD_PROGRESS_RECONCILABLE_STATES = frozenset({"pending", "active", "retiring"})


class DiscordTurnStartupFailure(RuntimeError):
    """Raised after a Discord turn startup failure has been surfaced to the user."""


async def _upsert_discord_progress_lease(
    service: Any,
    *,
    lease_id: str,
    managed_thread_id: str,
    execution_id: Optional[str],
    channel_id: str,
    message_id: str,
    source_message_id: Optional[str],
    state: str,
    progress_label: Optional[str],
) -> Any:
    upsert = getattr(service._store, "upsert_turn_progress_lease", None)
    if not callable(upsert):
        return None
    return await upsert(
        lease_id=lease_id,
        managed_thread_id=managed_thread_id,
        execution_id=execution_id,
        channel_id=channel_id,
        message_id=message_id,
        source_message_id=source_message_id,
        state=state,
        progress_label=progress_label,
    )


async def _get_discord_progress_lease(service: Any, *, lease_id: str) -> Any:
    getter = getattr(service._store, "get_turn_progress_lease", None)
    if not callable(getter):
        return None
    return await getter(lease_id=lease_id)


async def _list_discord_progress_leases(
    service: Any,
    *,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> list[Any]:
    lister = getattr(service._store, "list_turn_progress_leases", None)
    if not callable(lister):
        return []
    return list(
        await lister(
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            channel_id=channel_id,
            message_id=message_id,
        )
        or []
    )


async def _update_discord_progress_lease(
    service: Any,
    *,
    lease_id: str,
    execution_id: Optional[str] | object = ...,
    state: Optional[str] | object = ...,
    progress_label: Optional[str] | object = ...,
) -> Any:
    updater = getattr(service._store, "update_turn_progress_lease", None)
    if not callable(updater):
        return None
    kwargs: dict[str, Any] = {"lease_id": lease_id}
    if execution_id is not ...:
        kwargs["execution_id"] = execution_id
    if state is not ...:
        kwargs["state"] = state
    if progress_label is not ...:
        kwargs["progress_label"] = progress_label
    return await updater(**kwargs)


async def _delete_discord_progress_lease(service: Any, *, lease_id: str) -> None:
    deleter = getattr(service._store, "delete_turn_progress_lease", None)
    if not callable(deleter):
        return
    await deleter(lease_id=lease_id)


async def cleanup_discord_terminal_progress_leases(
    service: Any,
    *,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    note: str,
    record_prefix: str,
) -> int:
    leases = await _list_discord_progress_leases(
        service,
        managed_thread_id=managed_thread_id,
        execution_id=execution_id,
        channel_id=channel_id,
    )
    if not leases:
        return 0
    cleaned = 0
    for lease in leases:
        current_lease_id = _execution_field(lease, "lease_id")
        current_channel_id = _execution_field(lease, "channel_id")
        current_message_id = _execution_field(lease, "message_id")
        if not current_lease_id or not current_channel_id or not current_message_id:
            continue
        delete_safe = getattr(service, "_delete_channel_message_safe", None)
        try:
            deleted = (
                await delete_safe(
                    channel_id=current_channel_id,
                    message_id=current_message_id,
                    record_id=f"{record_prefix}:{current_lease_id}",
                )
                if callable(delete_safe)
                else False
            )
        except (
            DiscordTransientError,
            DiscordPermanentError,
            RuntimeError,
            ConnectionError,
            OSError,
        ):
            deleted = False
        deleted = deleted is not False
        if deleted:
            await _delete_discord_progress_lease(service, lease_id=current_lease_id)
            cleaned += 1
            continue
        retired = await _retire_discord_progress_message(
            service,
            channel_id=current_channel_id,
            message_id=current_message_id,
            note=note,
        )
        if retired:
            await _delete_discord_progress_lease(service, lease_id=current_lease_id)
            cleaned += 1
    return cleaned


async def _retire_discord_progress_message(
    service: Any,
    *,
    channel_id: str,
    message_id: str,
    note: str,
) -> bool:
    normalized_channel_id = str(channel_id or "").strip()
    normalized_message_id = str(message_id or "").strip()
    normalized_note = str(note or "").strip()
    if not normalized_channel_id or not normalized_message_id or not normalized_note:
        return False
    content = normalized_note
    fetch_message = getattr(service._rest, "get_channel_message", None)
    if callable(fetch_message):
        try:
            fetched = await fetch_message(
                channel_id=normalized_channel_id,
                message_id=normalized_message_id,
            )
        except (
            DiscordTransientError,
            RuntimeError,
            ConnectionError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            fetched = {}
        existing_content = str(fetched.get("content") or "").strip()
        if existing_content:
            lowered_existing = existing_content.lower()
            lowered_note = normalized_note.lower()
            if lowered_note not in lowered_existing:
                content = f"{existing_content.rstrip()}\n\n{normalized_note}"
            else:
                content = existing_content
    try:
        await service._rest.edit_channel_message(
            channel_id=normalized_channel_id,
            message_id=normalized_message_id,
            payload={
                "content": truncate_for_discord(
                    content,
                    max_len=max(int(service._config.max_message_length), 32),
                ),
                "components": [],
            },
        )
    except DiscordPermanentError as exc:
        if is_unknown_message_error(exc):
            return True
        raise
    except (DiscordTransientError, RuntimeError, ConnectionError, OSError):
        return False
    return True


def _orphaned_progress_note(*, startup: bool) -> str:
    if startup:
        return (
            "Status: this progress message lost its Discord worker during restart. "
            "Please retry if you still need a response."
        )
    return (
        "Status: this progress message lost its Discord worker and is no longer live. "
        "Please retry if needed."
    )


def _shutdown_progress_note() -> str:
    return (
        "Status: this progress message was interrupted during Discord shutdown and "
        "is no longer live. Please retry if needed."
    )


async def _apply_discord_progress_run_event(
    projector: ManagedThreadProgressProjector,
    run_event: Any,
    *,
    edit_progress: Any,
) -> None:
    if isinstance(run_event, TokenUsage):
        usage_payload = run_event.usage
        if isinstance(usage_payload, dict):
            projector.note_context_usage(_extract_context_usage_percent(usage_payload))
        return
    outcome = projector.apply_run_event(run_event)
    if not outcome.changed:
        return
    await edit_progress(
        force=outcome.force,
        remove_components=outcome.remove_components,
        render_mode=outcome.render_mode,
    )


def _resolve_discord_progress_reconcile_note(
    *,
    referenced_execution_id: Optional[str],
    latest_execution: Any,
    running_execution: Any,
    resolved_execution: Any,
    thread_missing: bool,
    failure_note: Optional[str],
    orphaned: bool,
    startup: bool,
) -> Optional[str]:
    if isinstance(failure_note, str) and failure_note.strip():
        return failure_note.strip()
    if thread_missing:
        return (
            "Status: this progress message no longer maps to an active managed thread."
        )
    if orphaned:
        return _orphaned_progress_note(startup=startup)
    if referenced_execution_id is None:
        return "Status: this turn failed before execution started."
    latest_execution_id = _execution_field(latest_execution, "execution_id")
    if latest_execution_id and latest_execution_id != referenced_execution_id:
        return "Status: this progress message belongs to an older turn. A newer turn is active."
    resolved_status = (_execution_field(resolved_execution, "status") or "").lower()
    if resolved_status == "ok":
        return "Status: this turn already completed."
    if resolved_status == "interrupted":
        return "Status: this turn was already stopped."
    if resolved_status == "error":
        return "Status: this turn already failed."
    if resolved_status == "queued":
        return "Status: this turn is queued and no longer has an active cancel surface."
    if resolved_status == "running":
        running_execution_id = _execution_field(running_execution, "execution_id")
        if running_execution_id == referenced_execution_id:
            return None
    return "Status: this turn is no longer active."


async def reconcile_discord_turn_progress_leases(
    service: Any,
    *,
    lease_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
    failure_note: Optional[str] = None,
    orphaned: bool = False,
    startup: bool = False,
) -> int:
    from .message_turns import build_discord_thread_orchestration_service

    leases: list[Any]
    if isinstance(lease_id, str) and lease_id.strip():
        lease = await _get_discord_progress_lease(service, lease_id=lease_id)
        leases = [lease] if lease is not None else []
    else:
        leases = await _list_discord_progress_leases(
            service,
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            channel_id=channel_id,
            message_id=message_id,
        )
    if not leases:
        return 0

    reconciled = 0
    orchestration_service = build_discord_thread_orchestration_service(service)
    get_thread_target = getattr(orchestration_service, "get_thread_target", None)
    get_latest_execution = getattr(orchestration_service, "get_latest_execution", None)
    get_running_execution = getattr(
        orchestration_service,
        "get_running_execution",
        None,
    )
    get_execution = getattr(orchestration_service, "get_execution", None)

    for lease in leases:
        current_lease_id = _execution_field(lease, "lease_id")
        current_thread_id = _execution_field(lease, "managed_thread_id")
        current_execution_id = _execution_field(lease, "execution_id")
        current_channel_id = _execution_field(lease, "channel_id")
        current_message_id = _execution_field(lease, "message_id")
        if (
            not current_lease_id
            or not current_thread_id
            or not current_channel_id
            or not current_message_id
        ):
            continue
        current_state = (_execution_field(lease, "state") or "").lower()
        if current_state and current_state not in _DISCORD_PROGRESS_RECONCILABLE_STATES:
            continue

        thread_missing = False
        resolved_thread = None
        if callable(get_thread_target):
            try:
                resolved_thread = get_thread_target(current_thread_id)
            except (RuntimeError, ValueError, TypeError, AttributeError, KeyError):
                resolved_thread = None
        if resolved_thread is None:
            thread_missing = True

        latest_execution_record = None
        if callable(get_latest_execution) and not thread_missing:
            with contextlib.suppress(
                RuntimeError, ValueError, TypeError, AttributeError, KeyError
            ):
                latest_execution_record = get_latest_execution(current_thread_id)
        running_execution_record = None
        if callable(get_running_execution) and not thread_missing:
            with contextlib.suppress(
                RuntimeError, ValueError, TypeError, AttributeError, KeyError
            ):
                running_execution_record = get_running_execution(current_thread_id)
        resolved_execution_record = None
        if (
            callable(get_execution)
            and not thread_missing
            and current_execution_id is not None
        ):
            with contextlib.suppress(
                RuntimeError, ValueError, TypeError, AttributeError, KeyError
            ):
                resolved_execution_record = get_execution(
                    current_thread_id,
                    current_execution_id,
                )
        if resolved_execution_record is None:
            resolved_execution_record = latest_execution_record

        note = _resolve_discord_progress_reconcile_note(
            referenced_execution_id=current_execution_id,
            latest_execution=latest_execution_record,
            running_execution=running_execution_record,
            resolved_execution=resolved_execution_record,
            thread_missing=thread_missing,
            failure_note=failure_note,
            orphaned=orphaned,
            startup=startup,
        )
        if note is None:
            await _update_discord_progress_lease(
                service,
                lease_id=current_lease_id,
                state="active",
            )
            continue
        await _update_discord_progress_lease(
            service,
            lease_id=current_lease_id,
            state="retiring",
        )
        retired = await _retire_discord_progress_message(
            service,
            channel_id=current_channel_id,
            message_id=current_message_id,
            note=note,
        )
        if retired:
            await _delete_discord_progress_lease(service, lease_id=current_lease_id)
            reconciled += 1
    return reconciled


async def clear_discord_turn_progress_leases(
    service: Any,
    *,
    lease_id: Optional[str] = None,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> int:
    leases: list[Any]
    if isinstance(lease_id, str) and lease_id.strip():
        lease = await _get_discord_progress_lease(service, lease_id=lease_id)
        leases = [lease] if lease is not None else []
    else:
        leases = await _list_discord_progress_leases(
            service,
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            channel_id=channel_id,
            message_id=message_id,
        )
    cleared = 0
    for lease in leases:
        current_lease_id = _execution_field(lease, "lease_id")
        if not current_lease_id:
            continue
        await _delete_discord_progress_lease(service, lease_id=current_lease_id)
        cleared += 1
    return cleared


def _discord_progress_lease_is_not_newer_than_terminal_turn(
    lease: Any,
    *,
    terminal_message_id: Optional[str],
    terminal_created_at: Optional[str],
) -> bool:
    lease_message_id = _execution_field(lease, "message_id")
    if (
        isinstance(lease_message_id, str)
        and lease_message_id.isdigit()
        and isinstance(terminal_message_id, str)
        and terminal_message_id.isdigit()
    ):
        return int(lease_message_id) <= int(terminal_message_id)
    return False


async def _reconcile_other_discord_turn_progress_leases(
    service: Any,
    *,
    managed_thread_id: Optional[str],
    keep_lease_id: Optional[str] = None,
    keep_message_id: Optional[str] = None,
    terminal_message_id: Optional[str] = None,
    terminal_created_at: Optional[str] = None,
) -> int:
    normalized_thread_id = str(managed_thread_id or "").strip()
    if not normalized_thread_id:
        return 0
    retained_lease_id = str(keep_lease_id or "").strip() or None
    retained_message_id = str(keep_message_id or "").strip() or None
    reconciled = 0
    for lease in await _list_discord_progress_leases(
        service,
        managed_thread_id=normalized_thread_id,
    ):
        current_lease_id = _execution_field(lease, "lease_id")
        current_message_id = _execution_field(lease, "message_id")
        if current_lease_id and current_lease_id == retained_lease_id:
            continue
        if current_message_id and current_message_id == retained_message_id:
            continue
        if current_message_id and current_message_id == terminal_message_id:
            continue
        if not _discord_progress_lease_is_not_newer_than_terminal_turn(
            lease,
            terminal_message_id=terminal_message_id,
            terminal_created_at=terminal_created_at,
        ):
            continue
        reconciled += await reconcile_discord_turn_progress_leases(
            service,
            lease_id=current_lease_id,
        )
    return reconciled


async def _acknowledge_discord_progress_reuse(
    service: Any,
    *,
    channel_id: str,
    message_id: str,
    acknowledgement: str,
) -> bool:
    try:
        await service._rest.edit_channel_message(
            channel_id=channel_id,
            message_id=message_id,
            payload={
                "content": truncate_for_discord(
                    format_discord_message(acknowledgement),
                    max_len=max(int(service._config.max_message_length), 32),
                ),
                "components": [],
            },
        )
    except DiscordPermanentError as exc:
        if is_unknown_message_error(exc):
            return False
        raise
    except (DiscordTransientError, RuntimeError, ConnectionError, OSError):
        return False
    return True


def _spawn_discord_background_task(
    service: Any,
    coro: Awaitable[None],
    *,
    await_on_shutdown: bool = False,
) -> asyncio.Task[Any]:
    spawn_task = getattr(service, "_spawn_task", None)
    if not callable(spawn_task):
        return cast(asyncio.Task[Any], asyncio.ensure_future(coro))
    if not await_on_shutdown:
        return cast(asyncio.Task[Any], spawn_task(coro))
    try:
        return cast(
            asyncio.Task[Any],
            spawn_task(coro, await_on_shutdown=True),
        )
    except TypeError as exc:
        if "await_on_shutdown" not in str(exc):
            raise
        return cast(asyncio.Task[Any], spawn_task(coro))


def _spawn_discord_progress_background_task(
    service: Any,
    coro: Awaitable[None],
    *,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    lease_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
    failure_note: Optional[str] = None,
    orphaned: bool = False,
    reconcile_on_cancel: bool = False,
    await_on_shutdown: bool = False,
) -> asyncio.Task[Any]:
    task = _spawn_discord_background_task(
        service,
        coro,
        await_on_shutdown=await_on_shutdown,
    )
    return bind_discord_progress_task_context(
        task,
        managed_thread_id=managed_thread_id,
        execution_id=execution_id,
        lease_id=lease_id,
        channel_id=channel_id,
        message_id=message_id,
        failure_note=failure_note,
        orphaned=orphaned,
        reconcile_on_cancel=reconcile_on_cancel,
    )
