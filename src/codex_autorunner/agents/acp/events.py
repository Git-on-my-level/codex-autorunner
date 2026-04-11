from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from ...core.acp_lifecycle import (
    analyze_acp_lifecycle_message,
)
from ...core.acp_lifecycle import (
    coerce_mapping as _coerce_mapping,
)
from ...core.acp_lifecycle import (
    extract_identifier as _extract_identifier,
)


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
    lifecycle = analyze_acp_lifecycle_message(message)
    method = lifecycle.method
    payload = lifecycle.payload
    session_id = lifecycle.session_id
    turn_id = lifecycle.turn_id

    if method in {"session/created", "session/loaded"}:
        session = (
            _coerce_mapping(payload.get("session")) if "session" in payload else payload
        )
        session_id = session_id or _extract_identifier(
            session, "sessionId", "session_id", "sessionID", "id"
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

    if lifecycle.normalized_kind == "turn_terminal":
        return ACPTurnTerminalEvent(
            kind="turn_terminal",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            status=lifecycle.terminal_status or "",
            final_output=lifecycle.assistant_text,
            error_message=lifecycle.error_message,
        )

    if method in {"session.status", "session/status"}:
        return ACPProgressEvent(
            kind="progress",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            message=lifecycle.progress_message,
        )

    if method == "session/update":
        update = _coerce_mapping(payload.get("update"))
        if lifecycle.normalized_kind == "output_delta":
            return ACPOutputDeltaEvent(
                kind="output_delta",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                delta=lifecycle.output_delta,
            )
        if lifecycle.normalized_kind == "progress":
            return ACPProgressEvent(
                kind="progress",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                message=lifecycle.progress_message,
            )
        if lifecycle.normalized_kind == "token_usage":
            return ACPTokenUsageEvent(
                kind="token_usage",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                usage=lifecycle.usage,
            )
        if lifecycle.normalized_kind == "session":
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
        if lifecycle.normalized_kind == "output_delta":
            return ACPOutputDeltaEvent(
                kind="output_delta",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                delta=lifecycle.output_delta,
            )
        if lifecycle.normalized_kind == "token_usage":
            return ACPTokenUsageEvent(
                kind="token_usage",
                method=method,
                session_id=session_id,
                turn_id=turn_id,
                payload=payload,
                raw_notification=raw_notification,
                usage=lifecycle.usage,
            )
        return ACPProgressEvent(
            kind="progress",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            message=lifecycle.progress_message,
        )

    if method in {"prompt/message", "turn/message"}:
        return ACPMessageEvent(
            kind="message",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            message=lifecycle.assistant_text,
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
            request_id=lifecycle.permission_request_id,
            description=lifecycle.permission_description,
            context=context,
        )

    if method == "token/usage":
        return ACPTokenUsageEvent(
            kind="token_usage",
            method=method,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
            raw_notification=raw_notification,
            usage=lifecycle.usage or payload,
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
