"""OpenCode protocol payload readers and schema-drift adapters.

This module owns all OpenCode session/message/part/permission/question/usage
field extraction and alias normalization.  Runtime and harness consume these
readers instead of hand-rolling dict traversal.

The authoritative schema baseline is committed under ``vendor/protocols/``.
Any new field aliases must be added here and covered by characterization tests
before production use.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional

from ...core.coercion import coerce_int
from .constants import (
    OPENCODE_CONTEXT_WINDOW_KEYS,
    OPENCODE_USAGE_CACHED_KEYS,
    OPENCODE_USAGE_INPUT_KEYS,
    OPENCODE_USAGE_OUTPUT_KEYS,
    OPENCODE_USAGE_REASONING_KEYS,
    OPENCODE_USAGE_TOTAL_KEYS,
)
from .event_decoder import parse_message_response as _decode_message_response
from .usage_decoder import extract_usage_field

PERMISSION_ALLOW = "allow"
PERMISSION_DENY = "deny"
PERMISSION_ASK = "ask"

OPENCODE_PERMISSION_ONCE = "once"
OPENCODE_PERMISSION_ALWAYS = "always"
OPENCODE_PERMISSION_REJECT = "reject"

OPENCODE_IDLE_STATUS_VALUES = {
    "idle",
    "done",
    "completed",
    "complete",
    "finished",
    "success",
}


@dataclass(frozen=True)
class OpenCodeMessageResult:
    text: str
    error: Optional[str] = None


def split_model_id(model: Optional[str]) -> Optional[dict[str, str]]:
    if not model or "/" not in model:
        return None
    provider_id, model_id = model.split("/", 1)
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


def build_turn_id(session_id: str) -> str:
    return f"{session_id}:{int(time.time() * 1000)}"


def _direct_session_id(
    payload: dict[str, Any], *, allow_fallback_id: bool
) -> Optional[str]:
    for key in ("sessionID", "sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if allow_fallback_id:
        value = payload.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _nested_session_id(
    payload: Any, *, allow_fallback_id: bool = False
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    session_id = _direct_session_id(payload, allow_fallback_id=allow_fallback_id)
    if session_id:
        return session_id
    session = payload.get("session")
    if isinstance(session, dict):
        return _nested_session_id(session, allow_fallback_id=True)
    return None


_NESTED_CONTAINER_KEYS = ("info", "properties", "response")
_PART_KEYS = ("part", "parts")


def _walk_nested_containers(
    payload: Any, *, include_parts: bool = False, _visited: frozenset[int] | None = None
) -> Iterator[dict[str, Any]]:
    if not isinstance(payload, dict):
        return
    if _visited is None:
        _visited = frozenset()
    obj_id = id(payload)
    if obj_id in _visited:
        return
    _visited = _visited | {obj_id}
    yield payload
    if include_parts:
        for pk in _PART_KEYS:
            part = payload.get(pk)
            if isinstance(part, dict):
                yield from _walk_nested_containers(
                    part, include_parts=True, _visited=_visited
                )
            elif isinstance(part, list):
                for entry in part:
                    if isinstance(entry, dict):
                        yield from _walk_nested_containers(
                            entry, include_parts=True, _visited=_visited
                        )
    for key in _NESTED_CONTAINER_KEYS:
        child = payload.get(key)
        if isinstance(child, dict):
            yield from _walk_nested_containers(
                child, include_parts=include_parts, _visited=_visited
            )
    model = payload.get("model")
    if isinstance(model, dict):
        yield model
    for key in ("modelInfo", "model_info", "modelConfig", "model_config"):
        mi = payload.get(key)
        if isinstance(mi, dict):
            yield mi


def extract_session_id(
    payload: Any, *, allow_fallback_id: bool = False
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    session_id = _direct_session_id(payload, allow_fallback_id=allow_fallback_id)
    if session_id:
        return session_id
    info = payload.get("info")
    session_id = _nested_session_id(info)
    if session_id:
        return session_id
    properties = payload.get("properties")
    if isinstance(properties, dict):
        session_id = _direct_session_id(properties, allow_fallback_id=False)
        if session_id:
            return session_id
        for key in ("info", "part", "item"):
            session_id = _nested_session_id(properties.get(key))
            if session_id:
                return session_id
    session = payload.get("session")
    if isinstance(session, dict):
        return _nested_session_id(session, allow_fallback_id=True)
    item = payload.get("item")
    session_id = _nested_session_id(item)
    if session_id:
        return session_id
    return None


def extract_turn_id(session_id: str, payload: Any) -> str:
    if isinstance(payload, dict):
        info = payload.get("info")
        if isinstance(info, dict):
            for key in ("id", "messageId", "message_id", "turn_id", "turnId"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("id", "messageId", "message_id", "turn_id", "turnId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return build_turn_id(session_id)


def extract_model_ids(payload: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    for container in (payload, payload.get("properties"), payload.get("info")):
        if not isinstance(container, dict):
            continue
        provider_id = (
            container.get("providerID")
            or container.get("providerId")
            or container.get("provider_id")
        )
        model_id = (
            container.get("modelID")
            or container.get("modelId")
            or container.get("model_id")
        )
        if (
            isinstance(provider_id, str)
            and provider_id.strip()
            and isinstance(model_id, str)
            and model_id.strip()
        ):
            return provider_id, model_id
    return None, None


def extract_error_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "error"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(error, str) and error:
        return error
    for key in ("detail", "message", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def extract_visible_message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts_raw = payload.get("parts")
    text_parts: list[str] = []
    if isinstance(parts_raw, list):
        for part in parts_raw:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text" or bool(part.get("ignored")):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
    return "".join(text_parts).strip()


def parse_message_response(payload: Any) -> OpenCodeMessageResult:
    decoded = _decode_message_response(payload)
    text = decoded.get("text")
    error = decoded.get("error")
    if isinstance(text, str) and text:
        return OpenCodeMessageResult(text=text.strip(), error=error)
    if not isinstance(payload, dict):
        return OpenCodeMessageResult(text="")
    info = payload.get("info")
    error = error or extract_error_text(info) or extract_error_text(payload)
    parts_raw = payload.get("parts")
    text_parts: list[str] = []
    if isinstance(parts_raw, list):
        for part in parts_raw:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
    return OpenCodeMessageResult(text="".join(text_parts).strip(), error=error)


def recover_last_assistant_message(
    payload: Any, *, prompt: Optional[str] = None
) -> OpenCodeMessageResult:
    messages_raw: Any = payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            messages_raw = data
    if not isinstance(messages_raw, list):
        return OpenCodeMessageResult(text="", error=None)

    for entry in reversed(messages_raw):
        if not isinstance(entry, dict):
            continue
        info = entry.get("info")
        role = info.get("role") if isinstance(info, dict) else entry.get("role")
        if role != "assistant":
            continue
        text = extract_visible_message_text(entry)
        if not text:
            parsed = parse_message_response(entry)
            text = parsed.text.strip() if parsed.text else ""
            error = parsed.error
        else:
            error = None
        if error is None:
            error = extract_error_text(info) or extract_error_text(entry)
        if prompt_echo_matches(text, prompt=prompt):
            continue
        if text or error:
            return OpenCodeMessageResult(text=text, error=error)
    return OpenCodeMessageResult(text="", error=None)


def prompt_echo_matches(text: Any, *, prompt: Optional[str]) -> bool:
    if not isinstance(text, str):
        return False
    normalized_text = text.strip()
    if not normalized_text:
        return False
    normalized_prompt = (
        prompt.strip() if isinstance(prompt, str) and prompt.strip() else None
    )
    return normalized_prompt is not None and normalized_text == normalized_prompt


def normalize_message_phase(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"commentary", "final_answer"}:
        return normalized
    return None


_PHASE_EXTRA_KEYS = ("message", "item")


def extract_message_phase(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for container in _walk_nested_containers(payload):
        if container is payload:
            phase = normalize_message_phase(container.get("phase"))
            if phase is not None:
                return phase
            continue
        phase = normalize_message_phase(container.get("phase"))
        if phase is not None:
            return phase
        for key in _PHASE_EXTRA_KEYS:
            child = container.get(key)
            if isinstance(child, dict):
                phase = normalize_message_phase(child.get("phase"))
                if phase is not None:
                    return phase
    for key in _PHASE_EXTRA_KEYS:
        child = payload.get(key)
        if isinstance(child, dict):
            phase = normalize_message_phase(child.get("phase"))
            if phase is not None:
                return phase
    return None


def extract_permission_request(payload: Any) -> tuple[Optional[str], dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, {}
    properties = payload.get("properties")
    if isinstance(properties, dict):
        request_id = properties.get("id") or properties.get("requestID")
        if isinstance(request_id, str) and request_id:
            return request_id, properties
    request_id = payload.get("id") or payload.get("requestID")
    if isinstance(request_id, str) and request_id:
        return request_id, payload
    return None, {}


def normalize_question_policy(policy: Optional[str]) -> str:
    if not policy:
        return "ignore"
    normalized = policy.strip().lower()
    if normalized in ("auto_first_option", "auto_first", "first", "first_option"):
        return "auto_first_option"
    if normalized in ("auto_unanswered", "unanswered", "empty"):
        return "auto_unanswered"
    if normalized in ("reject", "deny", "cancel"):
        return "reject"
    if normalized in ("ignore", "none"):
        return "ignore"
    return "ignore"


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    questions: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            questions.append(item)
        elif isinstance(item, str):
            questions.append({"text": item})
    return questions


def extract_question_request(payload: Any) -> tuple[Optional[str], dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, {}
    properties = payload.get("properties")
    base = properties if isinstance(properties, dict) else payload
    if not isinstance(base, dict):
        base = payload
    request_id = None
    for container in (base, payload):
        if not isinstance(container, dict):
            continue
        for key in ("id", "requestID", "requestId"):
            value = container.get(key)
            if isinstance(value, str) and value:
                request_id = value
                break
        if request_id:
            break
    questions = None
    for container in (base, payload):
        if not isinstance(container, dict):
            continue
        candidate = container.get("questions")
        if isinstance(candidate, list):
            questions = candidate
            break
    normalized = _normalize_questions(questions)
    props = dict(base)
    props["questions"] = normalized
    return request_id, props


def extract_question_option_label(option: Any) -> Optional[str]:
    if isinstance(option, str):
        return option.strip() or None
    if isinstance(option, dict):
        for key in ("label", "text", "value", "name", "id"):
            value = option.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_question_options(question: dict[str, Any]) -> list[str]:
    for key in ("options", "choices"):
        raw = question.get(key)
        if isinstance(raw, list):
            options = []
            for option in raw:
                label = extract_question_option_label(option)
                if label:
                    options.append(label)
            return options
    return []


def auto_answers_for_questions(
    questions: list[dict[str, Any]], policy: str
) -> list[list[str]]:
    if policy == "auto_unanswered":
        return [[] for _ in questions]
    answers: list[list[str]] = []
    for question in questions:
        options = extract_question_options(question)
        if options:
            answers.append([options[0]])
        else:
            answers.append([])
    return answers


def normalize_question_answers(answers: Any, *, question_count: int) -> list[list[str]]:
    if not isinstance(answers, list):
        normalized: list[list[str]] = []
    elif answers and all(isinstance(item, str) for item in answers):
        normalized = [[item for item in answers if isinstance(item, str)]]
    else:
        normalized = []
        for item in answers:
            if isinstance(item, list):
                normalized.append([entry for entry in item if isinstance(entry, str)])
            elif isinstance(item, str):
                normalized.append([item])
            else:
                normalized.append([])
    if question_count <= 0:
        return normalized
    if len(normalized) < question_count:
        normalized.extend([[] for _ in range(question_count - len(normalized))])
    return normalized[:question_count]


def summarize_question_answers(answers: list[list[str]]) -> list[str]:
    summary: list[str] = []
    for answer in answers:
        if not answer:
            summary.append("")
        elif len(answer) == 1:
            summary.append(answer[0])
        else:
            summary.append(", ".join(answer))
    return summary


def format_permission_prompt(payload: dict[str, Any]) -> str:
    lines = ["Approval required"]
    reason = payload.get("reason") or payload.get("message") or payload.get("detail")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    action = payload.get("action") or payload.get("tool")
    if isinstance(action, str) and action:
        lines.append(f"Action: {action}")
    target = payload.get("target") or payload.get("path")
    if isinstance(target, str) and target:
        lines.append(f"Target: {target}")
    return "\n".join(lines)


def map_approval_policy_to_permission(
    approval_policy: Optional[str], *, default: str = PERMISSION_ALLOW
) -> str:
    if approval_policy is None:
        return default
    normalized = approval_policy.strip().lower()
    if normalized in ("never", "allow", "approved", "approve"):
        return PERMISSION_ALLOW
    if normalized in ("deny", "reject", "blocked"):
        return PERMISSION_DENY
    if normalized in (
        "on-request",
        "on-failure",
        "on_failure",
        "onfailure",
        "unlesstrusted",
        "untrusted",
        "ask",
        "auto",
    ):
        return PERMISSION_ASK
    return default


def normalize_permission_decision(decision: Any) -> str:
    decision_norm = str(decision or "").strip().lower()
    if decision_norm in (
        "always",
        "accept_session",
        "accept-session",
        "allow_session",
        "allow-session",
        "session",
        "session_allow",
    ):
        return OPENCODE_PERMISSION_ALWAYS
    if decision_norm in (
        "allow",
        "approved",
        "approve",
        "accept",
        "accepted",
        "yes",
        "y",
        "ok",
        "okay",
        "true",
        "1",
    ):
        return OPENCODE_PERMISSION_ONCE
    if decision_norm in (
        "deny",
        "reject",
        "decline",
        "declined",
        "cancel",
        "no",
        "n",
        "false",
        "0",
    ):
        return OPENCODE_PERMISSION_REJECT
    return OPENCODE_PERMISSION_REJECT


def permission_policy_reply(policy: str) -> str:
    if policy == PERMISSION_ALLOW:
        return OPENCODE_PERMISSION_ONCE
    return OPENCODE_PERMISSION_REJECT


def extract_total_tokens(usage: dict[str, Any]) -> Optional[int]:
    total = extract_usage_field(usage, OPENCODE_USAGE_TOTAL_KEYS)
    if total is not None:
        return total
    input_tokens = extract_usage_field(usage, OPENCODE_USAGE_INPUT_KEYS) or 0
    cached_tokens = extract_usage_field(usage, OPENCODE_USAGE_CACHED_KEYS) or 0
    output_tokens = extract_usage_field(usage, OPENCODE_USAGE_OUTPUT_KEYS) or 0
    reasoning_tokens = extract_usage_field(usage, OPENCODE_USAGE_REASONING_KEYS) or 0
    if input_tokens or cached_tokens or output_tokens or reasoning_tokens:
        return input_tokens + output_tokens
    return None


_USAGE_DETAIL_SPECS: list[tuple[str, tuple[str, ...]]] = [
    ("inputTokens", OPENCODE_USAGE_INPUT_KEYS),
    ("cachedInputTokens", OPENCODE_USAGE_CACHED_KEYS),
    ("outputTokens", OPENCODE_USAGE_OUTPUT_KEYS),
    ("reasoningTokens", OPENCODE_USAGE_REASONING_KEYS),
]


def extract_usage_details(usage: dict[str, Any]) -> dict[str, int]:
    details: dict[str, int] = {}
    for output_key, keys in _USAGE_DETAIL_SPECS:
        value = extract_usage_field(usage, keys)
        if value is not None:
            details[output_key] = value
    return details


def extract_context_window(
    payload: Any, usage: Optional[dict[str, Any]]
) -> Optional[int]:
    containers: list[dict[str, Any]] = []
    if isinstance(usage, dict):
        containers.append(usage)
    if isinstance(payload, dict):
        seen: set[int] = set()
        for container in _walk_nested_containers(payload, include_parts=True):
            cid = id(container)
            if cid not in seen:
                seen.add(cid)
                containers.append(container)
    for container in containers:
        for key in OPENCODE_CONTEXT_WINDOW_KEYS:
            value = coerce_int(container.get(key))
            if value is not None and value > 0:
                return value
    return None


def _resolve_status_value(status: Any) -> Optional[str]:
    if isinstance(status, dict):
        value = status.get("type") or status.get("status")
    else:
        value = status
    if isinstance(value, str) and value:
        return value
    return None


def extract_status_type(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for container in (
        payload,
        payload.get("status"),
        payload.get("info"),
        payload.get("properties"),
    ):
        if not isinstance(container, dict):
            continue
        if container is payload:
            candidate = _resolve_status_value(container.get("status"))
        else:
            candidate = _resolve_status_value(container)
        if candidate is not None:
            return candidate
    properties = payload.get("properties")
    if isinstance(properties, dict):
        candidate = _resolve_status_value(properties.get("status"))
        if candidate is not None:
            return candidate
    return None


def status_is_idle(status_type: Optional[str]) -> bool:
    if not status_type:
        return False
    return status_type.strip().lower() in OPENCODE_IDLE_STATUS_VALUES


def normalize_message_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value if value != "" else None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                part_type = part.get("type")
                if isinstance(part_type, str) and part_type != "text":
                    continue
                part_text = part.get("text")
                if isinstance(part_text, str):
                    parts.append(part_text)
        joined = "".join(parts)
        return joined if joined != "" else None
    if isinstance(value, dict):
        for key in ("text", "message", "content", "parts"):
            nested_text = normalize_message_text(value.get(key))
            if nested_text:
                return nested_text
        return None
    return None


def extract_delta_text(params: dict[str, Any]) -> Optional[str]:
    for key in ("content", "delta", "text"):
        text = normalize_message_text(params.get(key))
        if text:
            return text
    properties = params.get("properties")
    if isinstance(properties, dict):
        delta = properties.get("delta")
        if delta is not None:
            text = normalize_message_text(delta)
            if text:
                return text
        part = properties.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text = normalize_message_text(part.get("text"))
            if text:
                return text
    message = params.get("message")
    if isinstance(message, dict):
        return extract_delta_text(message)
    item = params.get("item")
    if isinstance(item, dict):
        return extract_delta_text(item)
    return None


def extract_completed_text(params: dict[str, Any]) -> Optional[str]:
    item = params.get("item")
    if isinstance(item, dict):
        return normalize_message_text(item.get("content")) or normalize_message_text(
            item
        )
    result = params.get("result")
    if isinstance(result, dict):
        return normalize_message_text(result)
    return normalize_message_text(params)


def extract_message_info(params: dict[str, Any]) -> dict[str, Any]:
    info = params.get("info")
    if isinstance(info, dict):
        return info
    properties = params.get("properties")
    if isinstance(properties, dict):
        nested = properties.get("info")
        if isinstance(nested, dict):
            return nested
    return {}


def extract_part_and_delta(
    payload: Any,
) -> tuple[Optional[dict[str, Any]], Any]:
    """Extract (part_dict, delta_value) from a message-part event payload.

    Handles ``properties.part`` / ``properties.delta`` nesting as well as
    direct ``part`` / ``delta`` keys, centralising the drift-vulnerable
    property layout that ``message.part.updated`` and ``message.part.delta``
    events use.
    """
    if not isinstance(payload, dict):
        return None, None
    properties = payload.get("properties")
    if isinstance(properties, dict):
        part = properties.get("part")
        delta = properties.get("delta")
    else:
        part = payload.get("part")
        delta = payload.get("delta")
    part_dict = part if isinstance(part, dict) else None
    return part_dict, delta


def extract_delta_text_value(delta: Any) -> Optional[str]:
    """Extract text from a delta value that may be a dict with a ``text`` key or a plain string."""
    if isinstance(delta, dict):
        return delta.get("text")
    if isinstance(delta, str):
        return delta
    return None
