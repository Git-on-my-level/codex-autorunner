"""State and task-context helpers for Discord progress lease workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional, cast


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


def _get_discord_thread_queue_task_map(service: Any) -> dict[str, asyncio.Task[Any]]:
    return _discord_orchestration_state(service).thread_queue_tasks
