from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .event_decoder import APPROVAL_METHODS, decode_notification
from .ids import extract_turn_id
from .protocol_types import ApprovalRequest, NotificationResult, UserInputRequest


@dataclass(frozen=True)
class NormalizedResponse:
    request_id: str
    error: Optional[dict[str, Any]]
    result: Any


@dataclass(frozen=True)
class NormalizedServerRequest:
    method: str
    request_id: Any
    params: dict[str, Any]


@dataclass(frozen=True)
class NormalizedNotification:
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ApprovalRequestEnvelope:
    request_id: Any
    method: str
    params: dict[str, Any]
    request: ApprovalRequest
    raw_message: dict[str, Any]


@dataclass(frozen=True)
class UserInputRequestEnvelope:
    request_id: Any
    method: str
    params: dict[str, Any]
    request: UserInputRequest
    raw_message: dict[str, Any]


@dataclass(frozen=True)
class NotificationEnvelope:
    method: str
    params: dict[str, Any]
    notification: NotificationResult
    raw_message: dict[str, Any]


def normalize_response(message: dict[str, Any]) -> Optional[NormalizedResponse]:
    req_id_raw = message.get("id")
    if not isinstance(req_id_raw, (int, str)):
        return None
    req_id = str(req_id_raw) if isinstance(req_id_raw, int) else req_id_raw
    error = message.get("error")
    if error is not None and not isinstance(error, dict):
        error = None
    result = message.get("result")
    return NormalizedResponse(
        request_id=req_id,
        error=error,
        result=result,
    )


def normalize_server_request(
    message: dict[str, Any],
) -> Optional[NormalizedServerRequest]:
    method = message.get("method")
    if not isinstance(method, str):
        return None
    req_id = message.get("id")
    params_raw = message.get("params")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
    return NormalizedServerRequest(
        method=method,
        request_id=req_id,
        params=params,
    )


def normalize_notification(message: dict[str, Any]) -> Optional[NormalizedNotification]:
    method = message.get("method")
    if not isinstance(method, str):
        return None
    params_raw = message.get("params")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
    return NormalizedNotification(
        method=method,
        params=params,
    )


def normalize_approval_request(
    message: dict[str, Any],
) -> Optional[ApprovalRequestEnvelope]:
    normalized = normalize_server_request(message)
    if normalized is None or normalized.method not in APPROVAL_METHODS:
        return None
    decoded = decode_notification(message)
    if not isinstance(decoded, ApprovalRequest):
        return None
    return ApprovalRequestEnvelope(
        request_id=normalized.request_id,
        method=normalized.method,
        params=normalized.params,
        request=decoded,
        raw_message=message,
    )


def normalize_user_input_request(
    message: dict[str, Any],
) -> Optional[UserInputRequestEnvelope]:
    normalized = normalize_server_request(message)
    if normalized is None or normalized.method != "item/tool/requestUserInput":
        return None
    params = normalized.params
    questions_raw = params.get("questions")
    questions = (
        tuple(question for question in questions_raw if isinstance(question, dict))
        if isinstance(questions_raw, list)
        else ()
    )
    return UserInputRequestEnvelope(
        request_id=normalized.request_id,
        method=normalized.method,
        params=params,
        request=UserInputRequest(
            method=normalized.method,
            item_id=(
                str(params.get("itemId")).strip()
                if isinstance(params.get("itemId"), str) and params.get("itemId")
                else None
            ),
            turn_id=extract_turn_id(params),
            thread_id=(
                str(params.get("threadId")).strip()
                if isinstance(params.get("threadId"), str) and params.get("threadId")
                else None
            ),
            questions=questions,
            context=params,
        ),
        raw_message=message,
    )


def normalize_notification_envelope(
    message: dict[str, Any],
) -> Optional[NotificationEnvelope]:
    normalized = normalize_notification(message)
    if normalized is None:
        return None
    return NotificationEnvelope(
        method=normalized.method,
        params=normalized.params,
        notification=decode_notification(message),
        raw_message=message,
    )


class RawApprovalRequestAdapter:
    def __init__(
        self,
        handler: Optional[Callable[[dict[str, Any]], Awaitable[Any]]],
        *,
        default_decision: Any,
    ) -> None:
        self._handler = handler
        self._default_decision = default_decision

    async def decide(self, envelope: ApprovalRequestEnvelope) -> Any:
        if self._handler is None:
            return self._default_decision
        return await _maybe_await(self._handler(envelope.raw_message))


class RawUserInputRequestAdapter:
    def __init__(
        self,
        handler: Optional[Callable[[dict[str, Any]], Awaitable[Any]]],
        *,
        default_result_factory: Callable[[UserInputRequestEnvelope], dict[str, Any]],
    ) -> None:
        self._handler = handler
        self._default_result_factory = default_result_factory

    async def decide(self, envelope: UserInputRequestEnvelope) -> Any:
        if self._handler is None:
            return self._default_result_factory(envelope)
        result = await _maybe_await(self._handler(envelope.raw_message))
        if result is None:
            return self._default_result_factory(envelope)
        return result


class RawNotificationAdapter:
    def __init__(
        self,
        handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]],
    ) -> None:
        self._handler = handler

    async def notify(self, envelope: NotificationEnvelope) -> None:
        if self._handler is None:
            return
        await _maybe_await(self._handler(envelope.raw_message))


def extract_resume_snapshot(
    payload: Any, target_turn_id: str
) -> Optional[tuple[Optional[str], list[str], list[str], list[str], list[str]]]:
    if not isinstance(payload, dict):
        return None
    snapshot = _collect_turn_snapshot_data(payload, target_turn_id)
    if (
        not snapshot.found
        and not snapshot.agent_messages
        and not snapshot.commentary_messages
        and not snapshot.final_answer_messages
        and not snapshot.errors
        and snapshot.status is None
    ):
        return None
    return (
        snapshot.status,
        snapshot.agent_messages,
        snapshot.commentary_messages,
        snapshot.final_answer_messages,
        snapshot.errors,
    )


def _collect_turn_snapshot_data(
    payload: dict[str, Any], target_turn_id: str
) -> ResumeSnapshot:
    snapshot = ResumeSnapshot(
        status=None,
        agent_messages=[],
        commentary_messages=[],
        final_answer_messages=[],
        errors=[],
        found=False,
        target_turn_id=target_turn_id,
    )
    if _collect_turn_snapshot_from_entry(payload, snapshot):
        snapshot.found = True
    _collect_turn_snapshot_from_payload(payload, snapshot)
    if snapshot.status is None:
        snapshot.status = _extract_status_value(payload.get("status"))
    return snapshot


def _collect_turn_snapshot_from_payload(
    payload: dict[str, Any], snapshot: ResumeSnapshot
) -> None:
    _collect_turn_snapshot_from_turn_collections(
        payload, snapshot, ("turns", "data", "results")
    )
    _collect_thread_snapshot_payload(payload.get("thread"), snapshot)
    if _collect_turn_snapshot_from_entry(payload.get("turn"), snapshot):
        snapshot.found = True
    _collect_turn_items(payload.get("items"), snapshot)


def _collect_turn_snapshot_from_turn_collections(
    payload: dict[str, Any], snapshot: ResumeSnapshot, keys: tuple[str, ...]
) -> None:
    for key in keys:
        values = payload.get(key)
        if isinstance(values, list):
            _collect_turn_snapshot_from_list(values, snapshot)


def _collect_thread_snapshot_payload(thread: Any, snapshot: ResumeSnapshot) -> None:
    if not isinstance(thread, dict):
        return
    _collect_thread_turn_items(thread, snapshot)
    _collect_turn_snapshot_from_list(thread.get("turns"), snapshot)


def _collect_turn_items(items: Any, snapshot: ResumeSnapshot) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        _collect_turn_snapshot_from_item(item, snapshot)


def _collect_turn_snapshot_from_list(turns: Any, snapshot: ResumeSnapshot) -> None:
    if not isinstance(turns, list):
        return
    for turn in turns:
        if _collect_turn_snapshot_from_entry(turn, snapshot):
            snapshot.found = True


def _collect_turn_snapshot_from_entry(turn: Any, snapshot: ResumeSnapshot) -> bool:
    if not isinstance(turn, dict):
        return False
    if extract_turn_id(turn) != snapshot.target_turn_id:
        return False
    if snapshot.status is None:
        snapshot.status = _extract_status_value(turn.get("status"))
    _extend_snapshot_agent_messages(
        snapshot,
        _extract_agent_messages_from_container(turn, snapshot.target_turn_id),
    )
    snapshot.errors.extend(_extract_errors_from_container(turn))
    return True


def _collect_turn_snapshot_from_item(item: Any, snapshot: ResumeSnapshot) -> None:
    if not isinstance(item, dict):
        return
    item_turn_id = extract_turn_id(item)
    if item_turn_id != snapshot.target_turn_id:
        return
    text = _extract_agent_message_text(item)
    if text:
        _append_snapshot_agent_message(
            snapshot, text, phase=_extract_agent_message_phase(item)
        )


def _collect_thread_turn_items(
    thread: dict[str, Any], snapshot: ResumeSnapshot
) -> None:
    thread_items = thread.get("items")
    if not isinstance(thread_items, list):
        return
    for item in thread_items:
        _collect_turn_snapshot_from_item(item, snapshot)


def _extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def _extract_agent_message_text(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type not in (None, "output_text", "text", "message"):
                continue
            candidate = entry.get("text")
            if isinstance(candidate, str) and candidate.strip():
                parts.append(candidate)
        if parts:
            return "".join(parts)
    return None


def _extract_agent_message_phase(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    phase = item.get("phase")
    if not isinstance(phase, str):
        return None
    normalized = phase.strip().lower()
    if normalized in {"commentary", "final_answer"}:
        return normalized
    return None


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _extract_errors_from_container(container: Any) -> list[str]:
    if not isinstance(container, dict):
        return []
    errors: list[str] = []
    error_message = _extract_error_message(container)
    if error_message:
        errors.append(error_message)
    raw_errors = container.get("errors")
    if isinstance(raw_errors, list):
        for entry in raw_errors:
            if isinstance(entry, str) and entry.strip():
                errors.append(entry.strip())
            elif isinstance(entry, dict):
                extracted = _extract_error_message(entry)
                if extracted:
                    errors.append(extracted)
    return errors


def _extract_error_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message: Optional[str] = None
    details: Optional[str] = None
    if isinstance(error, dict):
        raw_message = error.get("message")
        if isinstance(raw_message, str):
            message = raw_message.strip() or None
        raw_details = error.get("additionalDetails") or error.get("details")
        if isinstance(raw_details, str):
            details = raw_details.strip() or None
    elif isinstance(error, str):
        message = error.strip() or None
    if message is None:
        fallback = payload.get("message")
        if isinstance(fallback, str):
            message = fallback.strip() or None
    if details and details != message:
        if message:
            return f"{message} ({details})"
        return details
    return message


def _extract_agent_messages_from_container(
    container: Any, target_turn_id: Optional[str]
) -> list[tuple[str, Optional[str]]]:
    if not isinstance(container, dict):
        return []
    agent_messages: list[tuple[str, Optional[str]]] = []
    for key in ("items", "messages"):
        entries = container.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_turn_id = extract_turn_id(entry)
            if entry_turn_id and target_turn_id and entry_turn_id != target_turn_id:
                continue
            text = _extract_agent_message_text(entry)
            if text:
                agent_messages.append((text, _extract_agent_message_phase(entry)))
            elif entry.get("role") == "assistant":
                fallback = entry.get("text")
                if isinstance(fallback, str) and fallback.strip():
                    agent_messages.append(
                        (fallback, _extract_agent_message_phase(entry))
                    )
    return agent_messages


def _append_snapshot_agent_message(
    snapshot: "ResumeSnapshot",
    text: str,
    *,
    phase: Optional[str],
) -> None:
    snapshot.agent_messages.append(text)
    if phase == "commentary":
        snapshot.commentary_messages.append(text)
    elif phase == "final_answer":
        snapshot.final_answer_messages.append(text)


def _extend_snapshot_agent_messages(
    snapshot: "ResumeSnapshot",
    messages: list[tuple[str, Optional[str]]],
) -> None:
    for text, phase in messages:
        _append_snapshot_agent_message(snapshot, text, phase=phase)


class ResumeSnapshot:
    __slots__ = (
        "status",
        "agent_messages",
        "commentary_messages",
        "final_answer_messages",
        "errors",
        "found",
        "target_turn_id",
    )

    def __init__(
        self,
        status: Optional[str],
        agent_messages: list[str],
        commentary_messages: list[str],
        final_answer_messages: list[str],
        errors: list[str],
        found: bool,
        target_turn_id: str,
    ) -> None:
        self.status = status
        self.agent_messages = agent_messages
        self.commentary_messages = commentary_messages
        self.final_answer_messages = final_answer_messages
        self.errors = errors
        self.found = found
        self.target_turn_id = target_turn_id


__all__ = [
    "NormalizedNotification",
    "NormalizedResponse",
    "NormalizedServerRequest",
    "ResumeSnapshot",
    "extract_resume_snapshot",
    "normalize_notification",
    "normalize_response",
    "normalize_server_request",
]
