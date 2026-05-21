"""Semantic runtime state events consumed by orchestration reducers.

Adapters and runtime decoders normalize protocol-specific ACP, OpenCode, and
transport payloads into these events. Reducers should apply these semantic
events and preserve raw payloads only for observability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from ..acp_lifecycle import (
    analyze_acp_lifecycle_message,
    extract_message_phase,
)
from ..acp_lifecycle import (
    extract_error_message as _extract_error_message,
)
from ..acp_lifecycle import (
    extract_message_text as _extract_message_text,
)
from ..acp_lifecycle import (
    extract_output_delta as _extract_output_delta,
)
from .codex_item_normalizers import (
    extract_agent_message_text as _shared_extract_agent_message_text,
)
from .codex_item_normalizers import (
    extract_codex_usage,
    is_commentary_agent_message,
)
from .runtime_payload_shapes import canonicalize_token_usage

RuntimeStateEventStatus = Literal["ok", "error", "interrupted"]


@dataclass(frozen=True)
class AssistantDelta:
    text: str
    source: str
    message_id: Optional[str] = None


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    source: str
    message_id: Optional[str] = None


@dataclass(frozen=True)
class TerminalSignal:
    status: RuntimeStateEventStatus
    source: str
    error: Optional[str] = None
    final_text: Optional[str] = None


@dataclass(frozen=True)
class TransportReturned:
    status: str
    assistant_text: str
    errors: tuple[str, ...]
    raw_events: tuple[Any, ...]


@dataclass(frozen=True)
class FailureSignal:
    error: str
    source: str


@dataclass(frozen=True)
class TokenUsage:
    usage: dict[str, Any]
    source: str


@dataclass(frozen=True)
class ProgressSignal:
    kind: str
    message: str
    source: str


RuntimeStateEvent = Union[
    AssistantDelta,
    AssistantMessage,
    TerminalSignal,
    TransportReturned,
    FailureSignal,
    TokenUsage,
    ProgressSignal,
]


def normalize_runtime_state_events(raw_event: Any) -> list[RuntimeStateEvent]:
    """Map protocol-specific runtime payloads into reducer-owned state events.

    Adapters and protocol decoders own wire shape handling. Runtime state reducers
    should consume the semantic events from this module and retain raw payloads
    only for observability and trace storage.
    """

    if not isinstance(raw_event, dict):
        return []
    message = raw_event.get("message")
    payload = message if isinstance(message, dict) else raw_event
    method = str(payload.get("method") or "").strip()
    params = payload.get("params")
    if not isinstance(params, dict):
        params = payload if isinstance(payload, dict) else {}
    if not method:
        return []

    events: list[RuntimeStateEvent] = []
    lifecycle = analyze_acp_lifecycle_message(payload)
    usage = extract_codex_usage(params)
    if not usage and lifecycle.usage:
        usage = dict(lifecycle.usage)
    if usage:
        canonical_usage = canonicalize_token_usage(usage)
        if canonical_usage:
            events.append(TokenUsage(usage=canonical_usage, source=method))

    assistant_message_text = None
    assistant_stream_text = None
    failure_message = None
    terminal_status: Optional[RuntimeStateEventStatus] = None
    method_lower = method.lower()
    if method in {"message.completed", "message.updated"}:
        role = _extract_message_role(params)
        if (
            role != "user"
            and str(params.get("phase") or "").strip().lower() != "commentary"
        ):
            assistant_message_text = _extract_message_text(params)
    elif method in {"prompt/message", "turn/message"}:
        if lifecycle.message_phase != "commentary":
            assistant_message_text = lifecycle.assistant_text
    elif lifecycle.runtime_terminal_status is not None:
        terminal_status = lifecycle.runtime_terminal_status
        assistant_message_text = lifecycle.assistant_text or None
        if terminal_status in {"error", "interrupted"}:
            failure_message = lifecycle.error_message or _extract_error_message(
                params,
                default="",
            )
    elif method in {"turn/failed", "turn/error", "error"}:
        failure_message = _extract_error_message(params)
        terminal_status = "error"
    elif method == "item/completed":
        item = params.get("item")
        if (
            isinstance(item, dict)
            and str(item.get("type") or "").strip() == "agentMessage"
            and not is_commentary_agent_message(item)
        ):
            assistant_message_text = _shared_extract_agent_message_text(item) or None

    if (
        assistant_message_text is None
        and (
            method
            in {
                "prompt/output",
                "prompt/delta",
                "prompt/progress",
                "turn/progress",
                "item/agentMessage/delta",
                "message.delta",
                "turn/streamDelta",
            }
            or "outputdelta" in method_lower
        )
        and extract_message_phase(params) != "commentary"
    ):
        assistant_stream_text = _extract_output_delta(params)
    if assistant_stream_text is None and method == "session/update":
        update = params.get("update")
        if isinstance(update, dict):
            update_kind = str(lifecycle.session_update_kind or "").strip()
            if (
                update_kind == "agent_message_chunk"
                and lifecycle.message_phase != "commentary"
            ):
                assistant_stream_text = _extract_output_delta(update)

    if assistant_stream_text:
        events.append(AssistantDelta(text=assistant_stream_text, source=method))
    if assistant_message_text:
        events.append(AssistantMessage(text=assistant_message_text, source=method))
    if failure_message:
        events.append(FailureSignal(error=failure_message, source=method))
    if terminal_status is not None:
        events.append(
            TerminalSignal(
                status=terminal_status,
                source=method,
                error=failure_message or None,
                final_text=assistant_message_text or None,
            )
        )
    if events:
        events.append(
            ProgressSignal(kind="runtime_method", message=method, source=method)
        )
    return events


def normalize_transport_returned(result: Any) -> TransportReturned:
    return TransportReturned(
        status=str(getattr(result, "status", "") or "").strip().lower(),
        assistant_text=str(getattr(result, "assistant_text", "") or ""),
        errors=tuple(
            str(error or "").strip()
            for error in (getattr(result, "errors", ()) or ())
            if str(error or "").strip()
        ),
        raw_events=tuple(getattr(result, "raw_events", ()) or ()),
    )


def _extract_message_role(params: dict[str, Any]) -> str:
    role = params.get("role")
    if isinstance(role, str):
        return role.strip().lower()
    message = params.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if isinstance(role, str):
            return role.strip().lower()
    return ""


__all__ = [
    "AssistantDelta",
    "AssistantMessage",
    "FailureSignal",
    "ProgressSignal",
    "RuntimeStateEvent",
    "RuntimeStateEventStatus",
    "TerminalSignal",
    "TokenUsage",
    "TransportReturned",
    "normalize_runtime_state_events",
    "normalize_transport_returned",
]
