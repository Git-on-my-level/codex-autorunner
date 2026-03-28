from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _normalize_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _extract_identifier(payload: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        normalized = _normalize_optional_text(value)
        if normalized:
            return normalized
    return None


@dataclass(frozen=True)
class ACPInitializeResult:
    server_name: Optional[str]
    server_version: Optional[str]
    protocol_version: Optional[str]
    capabilities: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, payload: Any) -> "ACPInitializeResult":
        result = _coerce_mapping(payload)
        server_info = _coerce_mapping(result.get("serverInfo"))
        return cls(
            server_name=_normalize_optional_text(server_info.get("name")),
            server_version=_normalize_optional_text(server_info.get("version")),
            protocol_version=_normalize_optional_text(result.get("protocolVersion")),
            capabilities=_coerce_mapping(result.get("capabilities")),
            raw=result,
        )


@dataclass(frozen=True)
class ACPSessionDescriptor:
    session_id: str
    title: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, payload: Any) -> "ACPSessionDescriptor":
        result = _coerce_mapping(payload)
        session = (
            _coerce_mapping(result.get("session")) if "session" in result else result
        )
        session_id = _extract_identifier(session, "sessionId", "session_id", "id")
        if not session_id:
            raise ValueError("ACP session payload is missing a session identifier")
        return cls(
            session_id=session_id,
            title=_normalize_optional_text(session.get("title")),
            raw=session,
        )


@dataclass(frozen=True)
class ACPPromptDescriptor:
    session_id: str
    turn_id: str
    status: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(
        cls,
        payload: Any,
        *,
        session_id: Optional[str] = None,
    ) -> "ACPPromptDescriptor":
        result = _coerce_mapping(payload)
        prompt = _coerce_mapping(result.get("prompt")) if "prompt" in result else result
        turn_id = _extract_identifier(
            prompt, "turnId", "turn_id", "promptId", "prompt_id", "id"
        )
        if not turn_id:
            raise ValueError("ACP prompt payload is missing a turn identifier")
        resolved_session_id = (
            session_id or _extract_identifier(prompt, "sessionId", "session_id") or ""
        )
        if not resolved_session_id:
            raise ValueError("ACP prompt payload is missing a session identifier")
        return cls(
            session_id=resolved_session_id,
            turn_id=turn_id,
            status=_normalize_optional_text(prompt.get("status")),
            raw=prompt,
        )


def coerce_session_list(payload: Any) -> list[ACPSessionDescriptor]:
    result = _coerce_mapping(payload)
    entries = result.get("sessions")
    if not isinstance(entries, list):
        entries = payload if isinstance(payload, list) else []
    sessions: list[ACPSessionDescriptor] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sessions.append(ACPSessionDescriptor.from_result(entry))
    return sessions


__all__ = [
    "ACPInitializeResult",
    "ACPPromptDescriptor",
    "ACPSessionDescriptor",
    "coerce_session_list",
]
