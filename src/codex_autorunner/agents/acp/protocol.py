from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ...core.text_utils import _normalize_optional_text


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
        if not server_info:
            server_info = _coerce_mapping(result.get("agentInfo"))
        return cls(
            server_name=_normalize_optional_text(server_info.get("name")),
            server_version=_normalize_optional_text(server_info.get("version")),
            protocol_version=_normalize_optional_text(result.get("protocolVersion")),
            capabilities=_coerce_mapping(
                result.get("capabilities") or result.get("agentCapabilities")
            ),
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


@dataclass(frozen=True)
class ACPOptionalMethodResult:
    supported: bool
    result: Optional[Any] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_optional_response(cls, value: Any) -> "ACPOptionalMethodResult":
        if value is None:
            return cls(supported=False)
        return cls(
            supported=True,
            result=value,
            raw=_coerce_mapping(value) if isinstance(value, dict) else {},
        )


@dataclass(frozen=True)
class ACPSessionForkResult:
    session_id: Optional[str]
    title: Optional[str] = None
    supported: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_optional_response(cls, value: Any) -> "ACPSessionForkResult":
        if value is None:
            return cls(session_id=None, supported=False)
        mapping = _coerce_mapping(value)
        session = (
            _coerce_mapping(mapping.get("session")) if "session" in mapping else mapping
        )
        session_id = _extract_identifier(session, "sessionId", "session_id", "id")
        return cls(
            session_id=session_id,
            title=_normalize_optional_text(session.get("title")),
            supported=True,
            raw=mapping,
        )


@dataclass(frozen=True)
class ACPSetModelResult:
    supported: bool
    model_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_optional_response(cls, value: Any) -> "ACPSetModelResult":
        if value is None:
            return cls(supported=False)
        mapping = _coerce_mapping(value)
        return cls(
            supported=True,
            model_id=_normalize_optional_text(
                mapping.get("modelId") or mapping.get("model_id")
            ),
            raw=mapping,
        )


@dataclass(frozen=True)
class ACPSetModeResult:
    supported: bool
    mode: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_optional_response(cls, value: Any) -> "ACPSetModeResult":
        if value is None:
            return cls(supported=False)
        mapping = _coerce_mapping(value)
        return cls(
            supported=True,
            mode=_normalize_optional_text(mapping.get("mode")),
            raw=mapping,
        )


@dataclass(frozen=True)
class ACPAdvertisedCommand:
    name: str
    description: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ACPSessionCapabilities:
    list_sessions: bool = False
    fork: bool = False
    set_model: bool = False
    set_mode: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def extract_session_capabilities(
    capabilities: dict[str, Any],
) -> ACPSessionCapabilities:
    if not isinstance(capabilities, dict):
        return ACPSessionCapabilities()
    session_caps = capabilities.get("sessionCapabilities")
    if not isinstance(session_caps, dict):
        return ACPSessionCapabilities()
    return ACPSessionCapabilities(
        list_sessions=("list" in session_caps),
        fork=("fork" in session_caps),
        set_model=("setModel" in session_caps or "set_model" in session_caps),
        set_mode=("setMode" in session_caps or "set_mode" in session_caps),
        raw=dict(session_caps),
    )


def extract_advertised_commands(
    capabilities: dict[str, Any],
) -> list[ACPAdvertisedCommand]:
    if not isinstance(capabilities, dict):
        return []
    commands_raw = capabilities.get("commands")
    if not isinstance(commands_raw, list):
        commands_raw = capabilities.get("slashCommands")
    if not isinstance(commands_raw, list):
        session_caps = capabilities.get("sessionCapabilities")
        if isinstance(session_caps, dict):
            commands_raw = session_caps.get("commands")
    if not isinstance(commands_raw, list):
        return []
    commands: list[ACPAdvertisedCommand] = []
    for entry in commands_raw:
        if not isinstance(entry, dict):
            continue
        name = _normalize_optional_text(entry.get("name") or entry.get("command"))
        if not name:
            continue
        commands.append(
            ACPAdvertisedCommand(
                name=name,
                description=_normalize_optional_text(
                    entry.get("description") or entry.get("desc")
                ),
                raw=dict(entry),
            )
        )
    return commands


__all__ = [
    "ACPAdvertisedCommand",
    "ACPInitializeResult",
    "ACPOptionalMethodResult",
    "ACPPromptDescriptor",
    "ACPSessionCapabilities",
    "ACPSessionDescriptor",
    "ACPSessionForkResult",
    "ACPSetModeResult",
    "ACPSetModelResult",
    "coerce_session_list",
    "extract_advertised_commands",
    "extract_session_capabilities",
]
