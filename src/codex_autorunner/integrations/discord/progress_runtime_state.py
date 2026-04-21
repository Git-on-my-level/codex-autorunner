from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional


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


def _get_discord_thread_queue_task_map(service: Any) -> dict[str, asyncio.Task[Any]]:
    return _discord_orchestration_state(service).thread_queue_tasks
