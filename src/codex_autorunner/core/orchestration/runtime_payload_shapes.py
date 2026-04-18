from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class TokenUsageShape:
    """Typed representation of token usage counters extracted from raw payloads.

    Accepts multiple key aliases (protocol drift) and normalizes to a single
    canonical shape.  Use ``to_dict()`` to emit the canonical dict form for
    backward-compatible consumers that expect ``dict[str, Any]``.
    """

    total_tokens: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    context_window: Optional[int] = None

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> TokenUsageShape:
        def _int_key(key: str, *aliases: str) -> Optional[int]:
            for k in (key, *aliases):
                v = data.get(k)
                if isinstance(v, int):
                    return v
            return None

        return cls(
            total_tokens=_int_key("totalTokens", "total_tokens"),
            input_tokens=_int_key("inputTokens", "input_tokens"),
            output_tokens=_int_key("outputTokens", "output_tokens"),
            cached_tokens=_int_key(
                "cachedInputTokens", "cached_tokens", "cached_input_tokens"
            ),
            reasoning_tokens=_int_key("reasoningTokens", "reasoning_tokens"),
            context_window=_int_key(
                "modelContextWindow", "context_window", "contextWindow"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.total_tokens is not None:
            result["totalTokens"] = self.total_tokens
        if self.input_tokens is not None:
            result["inputTokens"] = self.input_tokens
        if self.output_tokens is not None:
            result["outputTokens"] = self.output_tokens
        if self.cached_tokens is not None:
            result["cachedInputTokens"] = self.cached_tokens
        if self.reasoning_tokens is not None:
            result["reasoningTokens"] = self.reasoning_tokens
        if self.context_window is not None:
            result["modelContextWindow"] = self.context_window
        return result

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.total_tokens,
                self.input_tokens,
                self.output_tokens,
                self.cached_tokens,
                self.reasoning_tokens,
                self.context_window,
            )
        )


@dataclass(frozen=True)
class OpenCodeToolPartShape:
    """Typed representation of an OpenCode tool part extracted from a raw part dict.

    Centralizes all the field-alias resolution and input extraction that was
    previously spread across inline dict traversal in
    ``_normalize_opencode_tool_part``.
    """

    tool_name: str
    tool_id: str
    status: Optional[str] = None
    input_payload: dict[str, Any] = field(default_factory=dict)
    state_payload: dict[str, Any] = field(default_factory=dict)
    error: Optional[Any] = None

    @classmethod
    def from_raw_part(cls, part: dict[str, Any]) -> Optional[OpenCodeToolPartShape]:
        raw_name = part.get("tool") or part.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None

        tool_name = raw_name.strip()
        raw_id = part.get("callID") or part.get("id") or tool_name
        tool_id = str(raw_id).strip() if raw_id is not None else tool_name

        state_payload = part.get("state")
        if not isinstance(state_payload, dict):
            state_payload = {}

        raw_status = state_payload.get("status")
        status = (
            str(raw_status).strip().lower() if isinstance(raw_status, str) else None
        )

        input_payload = _extract_tool_input(part)
        return cls(
            tool_name=tool_name,
            tool_id=tool_id,
            status=status,
            input_payload=input_payload,
            state_payload=state_payload,
            error=state_payload.get("error"),
        )


def _extract_tool_input(part: dict[str, Any]) -> dict[str, Any]:
    input_payload: dict[str, Any] = {}
    for key in ("input", "command", "cmd", "script"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            input_payload[key] = value.strip()
            break
    if not input_payload:
        args = part.get("args") or part.get("arguments") or part.get("params")
        if isinstance(args, dict):
            for key in ("command", "cmd", "script", "input"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    input_payload[key] = value.strip()
                    break
        elif isinstance(args, str) and args.strip():
            input_payload["input"] = args.strip()
    return input_payload


__all__ = [
    "OpenCodeToolPartShape",
    "TokenUsageShape",
]
