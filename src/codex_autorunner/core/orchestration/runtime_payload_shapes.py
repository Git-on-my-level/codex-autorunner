from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


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
            total_tokens=_int_key("totalTokens", "total_tokens", "total"),
            input_tokens=_int_key(
                "inputTokens", "input_tokens", "input", "promptTokens", "prompt_tokens"
            ),
            output_tokens=_int_key(
                "outputTokens",
                "output_tokens",
                "output",
                "completionTokens",
                "completion_tokens",
            ),
            cached_tokens=_int_key(
                "cachedInputTokens",
                "cached_tokens",
                "cached_input_tokens",
                "cachedRead",
                "cached_read",
                "cacheRead",
                "cache_read",
            ),
            reasoning_tokens=_int_key(
                "reasoningTokens",
                "reasoning_tokens",
                "reasoning",
                "reasoningOutputTokens",
                "reasoning_output_tokens",
            ),
            context_window=_int_key(
                "modelContextWindow",
                "context_window",
                "contextWindow",
                "size",
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


def canonicalize_token_usage(raw: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize provider token/context usage into CAR's canonical shape.

    The canonical wire shape is either flat token counters or Codex-style
    ``last``/``total`` buckets, with ``modelContextWindow`` at the top level.
    ACP/Hermes native usage uses ``used``/``size``; OpenCode commonly emits flat
    counters plus model metadata; Codex already emits the bucketed form.
    """

    if not isinstance(raw, Mapping):
        return None

    result: dict[str, Any] = {}
    for key in ("providerID", "providerId", "provider", "modelID", "modelId", "model"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            result[key] = value

    last = _canonicalize_usage_bucket(raw.get("last"))
    total = _canonicalize_usage_bucket(raw.get("total"))
    if last:
        result["last"] = last
    if total:
        result["total"] = total

    flat_source = _usage_counter_source(raw)
    flat = _canonicalize_usage_bucket(flat_source)
    if flat:
        if last or total:
            for key, value in flat.items():
                if key == "modelContextWindow":
                    continue
                result.setdefault(key, value)
        else:
            result.update(flat)

    context_window = _first_int(
        raw,
        "modelContextWindow",
        "contextWindow",
        "context_window",
        "contextLength",
        "context_length",
        "size",
    )
    if context_window is None:
        context_window = _bucket_context_window(last) or _bucket_context_window(total)
    if context_window is not None and context_window > 0:
        result["modelContextWindow"] = context_window

    return result or None


def _usage_counter_source(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        return usage
    token_usage = raw.get("tokenUsage")
    if isinstance(token_usage, Mapping):
        return token_usage
    return raw


def _canonicalize_usage_bucket(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    shape = TokenUsageShape.from_raw(dict(raw))
    used = _first_int(raw, "used", "usageUsed", "tokensUsed")
    total_tokens = shape.total_tokens
    if total_tokens is None and used is not None:
        total_tokens = used
    if total_tokens is None:
        parts = [
            value
            for value in (
                shape.input_tokens,
                shape.cached_tokens,
                shape.output_tokens,
                shape.reasoning_tokens,
            )
            if isinstance(value, int)
        ]
        if parts:
            total_tokens = sum(parts)
    if total_tokens != shape.total_tokens:
        shape = TokenUsageShape(
            total_tokens=total_tokens,
            input_tokens=shape.input_tokens,
            output_tokens=shape.output_tokens,
            cached_tokens=shape.cached_tokens,
            reasoning_tokens=shape.reasoning_tokens,
            context_window=shape.context_window,
        )
    result = shape.to_dict()
    return result or None


def _bucket_context_window(bucket: Optional[dict[str, Any]]) -> Optional[int]:
    if not bucket:
        return None
    value = bucket.get("modelContextWindow")
    return value if isinstance(value, int) else None


def _first_int(raw: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int):
            return value
    return None


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
    for key in ("input", "args", "arguments", "params"):
        value = part.get(key)
        payload = _coerce_tool_input_payload(value)
        if payload:
            return payload

    state = part.get("state")
    if isinstance(state, dict):
        for key in ("input", "args", "arguments", "params"):
            payload = _coerce_tool_input_payload(state.get(key))
            if payload:
                return payload

    for key in ("input", "command", "cmd", "script"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return {key: value.strip()}
    return {}


def _coerce_tool_input_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        return {"input": value.strip()}
    return {}


__all__ = [
    "canonicalize_token_usage",
    "OpenCodeToolPartShape",
    "TokenUsageShape",
]
