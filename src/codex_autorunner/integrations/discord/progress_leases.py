from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Optional, cast

from .errors import (
    DiscordPermanentError,
    DiscordTransientError,
    is_unknown_message_error,
)
from .rendering import (
    format_discord_message,
    truncate_for_discord,
)

_logger = logging.getLogger(__name__)

_DISCORD_PROGRESS_LIVE_STATES = frozenset({"pending", "active"})
_DISCORD_PROGRESS_RECONCILABLE_STATES = frozenset({"pending", "active", "retiring"})


class DiscordTurnStartupFailure(RuntimeError):
    """Raised after a Discord turn startup failure has been surfaced to the user."""


@dataclass(frozen=True)
class _DiscordProgressReuseRequest:
    source_message_id: str
    acknowledgement: str


@dataclass(frozen=True)
class _DiscordReusableProgressMessage:
    source_message_id: str
    channel_id: str
    message_id: str


@dataclass
class _DiscordOrchestrationState:
    progress_reuse_requests: dict[str, _DiscordProgressReuseRequest]
    reusable_progress_messages: dict[str, _DiscordReusableProgressMessage]
    thread_queue_tasks: dict[str, asyncio.Task[Any]]


@dataclass
class _DiscordTurnExecutionSupervision:
    service: Any
    channel_id: str
    task_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._set_text_field("channel_id", self.channel_id)

    def _set_text_field(self, key: str, value: Optional[str]) -> None:
        normalized = str(value or "").strip()
        if normalized:
            self.task_context[key] = normalized
            return
        self.task_context.pop(key, None)

    def _set_bool_field(self, key: str, value: bool) -> None:
        if value:
            self.task_context[key] = True
            return
        self.task_context.pop(key, None)

    def bind_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        cast(Any, task)._discord_progress_task_context = self.task_context
        return task

    def set_managed_thread_id(self, managed_thread_id: Optional[str]) -> None:
        self._set_text_field("managed_thread_id", managed_thread_id)

    def set_execution_id(self, execution_id: Optional[str]) -> None:
        self._set_text_field("execution_id", execution_id)

    def set_lease_id(self, lease_id: Optional[str]) -> None:
        self._set_text_field("lease_id", lease_id)

    def set_message_id(self, message_id: Optional[str]) -> None:
        self._set_text_field("message_id", message_id)

    def set_failure_note(self, failure_note: Optional[str]) -> None:
        self._set_text_field("failure_note", failure_note)

    def set_shutdown_note(self, shutdown_note: Optional[str]) -> None:
        self._set_text_field("shutdown_note", shutdown_note)

    def set_orphaned(self, orphaned: bool) -> None:
        self._set_bool_field("orphaned", orphaned)

    def clear_progress_tracking(self, *, keep_execution_id: bool = True) -> None:
        self.task_context.pop("lease_id", None)
        self.task_context.pop("message_id", None)
        if not keep_execution_id:
            self.task_context.pop("execution_id", None)

    async def reconcile_failure(
        self,
        *,
        failure_note: Optional[str] = None,
        allow_channel_fallback: bool = True,
    ) -> int:
        context = dict(self.task_context)
        if isinstance(failure_note, str) and failure_note.strip():
            context["failure_note"] = failure_note.strip()
        reconciler = getattr(self.service, "_reconcile_background_task_failure", None)
        if not callable(reconciler):
            return 0
        return int(
            await reconciler(
                context,
                allow_channel_fallback=allow_channel_fallback,
            )
            or 0
        )


def _discord_orchestration_state(service: Any) -> _DiscordOrchestrationState:
    requests = getattr(service, "_discord_turn_progress_reuse_requests", None)
    if not isinstance(requests, dict):
        requests = {}
        service._discord_turn_progress_reuse_requests = requests
    messages = getattr(service, "_discord_reusable_progress_messages", None)
    if not isinstance(messages, dict):
        messages = {}
        service._discord_reusable_progress_messages = messages
    task_map = getattr(service, "_discord_thread_queue_tasks", None)
    if not isinstance(task_map, dict):
        task_map = {}
        service._discord_thread_queue_tasks = task_map
        service._discord_managed_thread_queue_tasks = task_map
    return _DiscordOrchestrationState(
        progress_reuse_requests=requests,
        reusable_progress_messages=messages,
        thread_queue_tasks=task_map,
    )


def _get_discord_progress_reuse_requests(
    service: Any,
) -> dict[str, _DiscordProgressReuseRequest]:
    return _discord_orchestration_state(service).progress_reuse_requests


def _get_discord_reusable_progress_messages(
    service: Any,
) -> dict[str, _DiscordReusableProgressMessage]:
    return _discord_orchestration_state(service).reusable_progress_messages


def request_discord_turn_progress_reuse(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: str,
    acknowledgement: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    normalized_acknowledgement = str(acknowledgement or "").strip()
    if (
        not normalized_thread_target_id
        or not normalized_source_message_id
        or not normalized_acknowledgement
    ):
        return
    _get_discord_progress_reuse_requests(service)[normalized_thread_target_id] = (
        _DiscordProgressReuseRequest(
            source_message_id=normalized_source_message_id,
            acknowledgement=normalized_acknowledgement,
        )
    )


def clear_discord_turn_progress_reuse(
    service: Any,
    *,
    thread_target_id: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    if not normalized_thread_target_id:
        return
    _get_discord_progress_reuse_requests(service).pop(normalized_thread_target_id, None)
    _get_discord_reusable_progress_messages(service).pop(
        normalized_thread_target_id, None
    )


def _peek_discord_progress_reuse_request(
    service: Any,
    *,
    thread_target_id: str,
) -> Optional[_DiscordProgressReuseRequest]:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    if not normalized_thread_target_id:
        return None
    request = _get_discord_progress_reuse_requests(service).get(
        normalized_thread_target_id
    )
    if isinstance(request, _DiscordProgressReuseRequest):
        return request
    return None


def _stash_discord_reusable_progress_message(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: str,
    channel_id: str,
    message_id: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    normalized_channel_id = str(channel_id or "").strip()
    normalized_message_id = str(message_id or "").strip()
    if (
        not normalized_thread_target_id
        or not normalized_source_message_id
        or not normalized_channel_id
        or not normalized_message_id
    ):
        return
    _get_discord_reusable_progress_messages(service)[normalized_thread_target_id] = (
        _DiscordReusableProgressMessage(
            source_message_id=normalized_source_message_id,
            channel_id=normalized_channel_id,
            message_id=normalized_message_id,
        )
    )


def _claim_discord_reusable_progress_message(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: Optional[str],
) -> Optional[str]:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    if not normalized_thread_target_id or not normalized_source_message_id:
        return None
    requests = _get_discord_progress_reuse_requests(service)
    request = requests.get(normalized_thread_target_id)
    if isinstance(request, _DiscordProgressReuseRequest):
        if request.source_message_id != normalized_source_message_id:
            return None
        requests.pop(normalized_thread_target_id, None)
    reusable = _get_discord_reusable_progress_messages(service).pop(
        normalized_thread_target_id, None
    )
    if (
        isinstance(reusable, _DiscordReusableProgressMessage)
        and reusable.source_message_id == normalized_source_message_id
    ):
        return reusable.message_id
    return None


def _execution_field(record: Any, field: str) -> Optional[str]:
    if isinstance(record, dict):
        value = record.get(field)
    else:
        value = getattr(record, field, None)
    normalized = str(value or "").strip()
    return normalized or None


def _progress_task_context(
    *,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    lease_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
    failure_note: Optional[str] = None,
    shutdown_note: Optional[str] = None,
    orphaned: bool = False,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if isinstance(managed_thread_id, str) and managed_thread_id.strip():
        context["managed_thread_id"] = managed_thread_id.strip()
    if isinstance(execution_id, str) and execution_id.strip():
        context["execution_id"] = execution_id.strip()
    if isinstance(lease_id, str) and lease_id.strip():
        context["lease_id"] = lease_id.strip()
    if isinstance(channel_id, str) and channel_id.strip():
        context["channel_id"] = channel_id.strip()
    if isinstance(message_id, str) and message_id.strip():
        context["message_id"] = message_id.strip()
    if isinstance(failure_note, str) and failure_note.strip():
        context["failure_note"] = failure_note.strip()
    if isinstance(shutdown_note, str) and shutdown_note.strip():
        context["shutdown_note"] = shutdown_note.strip()
    if orphaned:
        context["orphaned"] = True
    return context


def bind_discord_progress_task_context(
    task: asyncio.Task[Any],
    *,
    managed_thread_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    lease_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    message_id: Optional[str] = None,
    failure_note: Optional[str] = None,
    shutdown_note: Optional[str] = None,
    orphaned: bool = False,
) -> asyncio.Task[Any]:
    context = _progress_task_context(
        managed_thread_id=managed_thread_id,
        execution_id=execution_id,
        lease_id=lease_id,
        channel_id=channel_id,
        message_id=message_id,
        failure_note=failure_note,
        shutdown_note=shutdown_note,
        orphaned=orphaned,
    )
    if context:
        cast(Any, task)._discord_progress_task_context = context
    return task


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
        return False
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
    )


def _get_discord_thread_queue_task_map(service: Any) -> dict[str, asyncio.Task[Any]]:
    return _discord_orchestration_state(service).thread_queue_tasks
