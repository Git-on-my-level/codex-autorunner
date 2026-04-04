from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from ...core.text_utils import _normalize_optional_text


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _extract_identifier(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        normalized = _normalize_optional_text(value)
        if normalized:
            return normalized
    return None


def _extract_text(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _session_update_kind(payload: Mapping[str, Any]) -> Optional[str]:
    value = payload.get("sessionUpdate")
    if value is None:
        value = payload.get("session_update")
    return _normalize_optional_text(value)


def _permission_description(payload: Mapping[str, Any]) -> str:
    description = _extract_text(payload, "description", "message")
    if description:
        return description
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, Mapping):
        return ""
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, Mapping):
        command = raw_input.get("command")
        if isinstance(command, list):
            normalized = " ".join(str(part) for part in command).strip()
            if normalized:
                return normalized
        if isinstance(command, str) and command.strip():
            return command
    kind = _normalize_optional_text(tool_call.get("kind"))
    return kind or ""


@dataclass(frozen=True)
class ACPEventEnvelope:
    kind: str
    method: str
    session_id: Optional[str]
    turn_id: Optional[str]
    payload: dict[str, Any] = field(default_factory=dict)
    raw_notification: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ACPSessionEvent(ACPEventEnvelope):
    action: str = ""
    session: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ACPTurnStartedEvent(ACPEventEnvelope):
    pass


@dataclass(frozen=True)
class ACPOutputDeltaEvent(ACPEventEnvelope):
    delta: str = ""


@dataclass(frozen=True)
class ACPMessageEvent(ACPEventEnvelope):
    message: str = ""


@dataclass(frozen=True)
class ACPProgressEvent(ACPEventEnvelope):
    message: str = ""


@dataclass(frozen=True)
class ACPPermissionRequestEvent(ACPEventEnvelope):
    request_id: str = ""
    description: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ACPTokenUsageEvent(ACPEventEnvelope):
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ACPTurnTerminalEvent(ACPEventEnvelope):
    status: str = ""
    final_output: str = ""
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ACPUnknownEvent(ACPEventEnvelope):
    pass


ACPEvent = Union[
    ACPSessionEvent,
    ACPTurnStartedEvent,
    ACPOutputDeltaEvent,
    ACPMessageEvent,
    ACPProgressEvent,
    ACPPermissionRequestEvent,
    ACPTokenUsageEvent,
    ACPTurnTerminalEvent,
    ACPUnknownEvent,
]


def normalize_notification(message: Mapping[str, Any]) -> ACPEvent:
    raw_notification = dict(message)
    method = _normalize_optional_text(message.get("method")) or ""
    payload = _coerce_mapping(message.get("params"))
    session_id = _extract_identifier(payload, "sessionId", "session_id")
    turn_id = _extract_identifier(
        payload,
        "turnId",
        "turn_id",
        "promptId",
        "prompt_id",
    )

    if method in {"session/created", "session/loaded"}:
        session = (
            _coerce_mapping(payload.get("session")) if "session" in payload else payload
        )
        session_id = session_id or _extract_identifier(
            session, "sessionId", "session_id", "id"
        )
        action = method.rsplit("/", 1)[-1]
        return ACPSessionEvent(
            kind="session",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            action=action,
            session=session,
        )

    if method == "session/update":
        update = _coerce_mapping(payload.get("update"))
        update_kind = _session_update_kind(update) or ""
        content = _coerce_mapping(update.get("content"))
        text = _extract_text(content, "text") or ""
        if update_kind == "agent_message_chunk":
            return ACPOutputDeltaEvent(
                kind="output_delta",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                delta=text,
            )
        if update_kind == "agent_thought_chunk":
            return ACPProgressEvent(
                kind="progress",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                message=text,
            )
        if update_kind == "usage_update":
            return ACPTokenUsageEvent(
                kind="token_usage",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                usage=update,
            )
        if update_kind == "session_info_update":
            return ACPSessionEvent(
                kind="session",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                action="updated",
                session=update,
            )

    if method in {"prompt/started", "turn/started"}:
        return ACPTurnStartedEvent(
            kind="turn_started",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
        )

    if method in {"prompt/output", "prompt/delta", "prompt/progress", "turn/progress"}:
        delta = _extract_text(payload, "delta", "textDelta", "text_delta")
        if delta is not None:
            return ACPOutputDeltaEvent(
                kind="output_delta",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                delta=delta,
            )
        usage = _coerce_mapping(payload.get("usage"))
        if usage:
            return ACPTokenUsageEvent(
                kind="token_usage",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                usage=usage,
            )
        return ACPProgressEvent(
            kind="progress",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            message=_extract_text(payload, "message", "status") or "",
        )

    if method in {"prompt/message", "turn/message"}:
        return ACPMessageEvent(
            kind="message",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            message=_extract_text(payload, "message", "finalOutput", "final_output")
            or "",
        )

    if method in {"permission/requested", "session/request_permission"}:
        context = _coerce_mapping(payload.get("context"))
        if not context:
            context = {
                key: value
                for key, value in payload.items()
                if key not in {"requestId", "request_id", "description", "message"}
            }
        return ACPPermissionRequestEvent(
            kind="permission_requested",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            request_id=(
                _extract_identifier(payload, "requestId", "request_id")
                or _normalize_optional_text(raw_notification.get("id"))
                or ""
            ),
            description=_permission_description(payload),
            context=context,
        )

    if method in {
        "prompt/completed",
        "prompt/cancelled",
        "turn/completed",
        "turn/cancelled",
    }:
        status = _normalize_optional_text(payload.get("status"))
        if not status:
            status = "cancelled" if method.endswith("cancelled") else "completed"
        return ACPTurnTerminalEvent(
            kind="turn_terminal",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            status=status,
            final_output=_extract_text(
                payload, "finalOutput", "final_output", "message"
            )
            or "",
        )

    if method in {"prompt/failed", "turn/failed"}:
        return ACPTurnTerminalEvent(
            kind="turn_terminal",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            status=_normalize_optional_text(payload.get("status")) or "failed",
            final_output="",
            error_message=_extract_text(payload, "error", "message"),
        )

    if method == "token/usage":
        return ACPTokenUsageEvent(
            kind="token_usage",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            usage=_coerce_mapping(payload.get("usage")) or payload,
        )

    return ACPUnknownEvent(
        kind="unknown",
        method=method,
        session_id=session_id,
        turn_id=turn_id,
        payload=payload,
        raw_notification=raw_notification,
    )


__all__ = [
    "ACPEvent",
    "ACPEventEnvelope",
    "ACPMessageEvent",
    "ACPOutputDeltaEvent",
    "ACPPermissionRequestEvent",
    "ACPProgressEvent",
    "ACPSessionEvent",
    "ACPTokenUsageEvent",
    "ACPTurnStartedEvent",
    "ACPTurnTerminalEvent",
    "ACPUnknownEvent",
    "normalize_notification",
]
