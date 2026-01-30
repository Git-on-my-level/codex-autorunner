from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    MutableMapping,
    Optional,
    cast,
)

import httpx

from ...core.logging_utils import log_event
from ...core.utils import infer_home_from_workspace
from .events import SSEEvent

PermissionDecision = str
PermissionHandler = Callable[[str, dict[str, Any]], Awaitable[PermissionDecision]]
QuestionHandler = Callable[[str, dict[str, Any]], Awaitable[Optional[list[list[str]]]]]
PartHandler = Callable[[str, dict[str, Any], Optional[str]], Awaitable[None]]

PERMISSION_ALLOW = "allow"
PERMISSION_DENY = "deny"
PERMISSION_ASK = "ask"

OPENCODE_PERMISSION_ONCE = "once"
OPENCODE_PERMISSION_ALWAYS = "always"
OPENCODE_PERMISSION_REJECT = "reject"

_OPENCODE_STREAM_STALL_TIMEOUT_SECONDS = 60.0
_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)
_OPENCODE_IDLE_STATUS_VALUES = {
    "idle",
    "done",
    "completed",
    "complete",
    "finished",
    "success",
}
_OPENCODE_USAGE_TOTAL_KEYS = ("totalTokens", "total_tokens", "total")
_OPENCODE_USAGE_INPUT_KEYS = (
    "inputTokens",
    "input_tokens",
    "promptTokens",
    "prompt_tokens",
)
_OPENCODE_USAGE_CACHED_KEYS = (
    "cachedTokens",
    "cached_tokens",
    "cachedInputTokens",
    "cached_input_tokens",
)
_OPENCODE_USAGE_OUTPUT_KEYS = (
    "outputTokens",
    "output_tokens",
    "completionTokens",
    "completion_tokens",
)
_OPENCODE_USAGE_REASONING_KEYS = (
    "reasoningTokens",
    "reasoning_tokens",
    "reasoningOutputTokens",
    "reasoning_output_tokens",
)
_OPENCODE_CONTEXT_WINDOW_KEYS = (
    "modelContextWindow",
    "contextWindow",
    "context_window",
    "contextWindowSize",
    "context_window_size",
    "contextLength",
    "context_length",
    "maxTokens",
    "max_tokens",
)
_OPENCODE_MODEL_CONTEXT_KEYS = ("context",) + _OPENCODE_CONTEXT_WINDOW_KEYS


@dataclass(frozen=True)
class OpenCodeMessageResult:
    text: str
    error: Optional[str] = None


@dataclass(frozen=True)
class OpenCodeTurnOutput:
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


def extract_session_id(
    payload: Any, *, allow_fallback_id: bool = False
) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if allow_fallback_id:
        value = payload.get("id")
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        for key in ("sessionID", "sessionId", "session_id"):
            value = properties.get(key)
            if isinstance(value, str) and value:
                return value
        part = properties.get("part")
        if isinstance(part, dict):
            for key in ("sessionID", "sessionId", "session_id"):
                value = part.get(key)
                if isinstance(value, str) and value:
                    return value
    session = payload.get("session")
    if isinstance(session, dict):
        return extract_session_id(session, allow_fallback_id=True)
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


def _extract_model_ids(payload: Any) -> tuple[Optional[str], Optional[str]]:
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


def parse_message_response(payload: Any) -> OpenCodeMessageResult:
    if not isinstance(payload, dict):
        return OpenCodeMessageResult(text="")
    info = payload.get("info")
    error = _extract_error_text(info) or _extract_error_text(payload)
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


def _extract_error_text(payload: Any) -> Optional[str]:
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


def _extract_permission_request(payload: Any) -> tuple[Optional[str], dict[str, Any]]:
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


def _normalize_question_policy(policy: Optional[str]) -> str:
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


def _extract_question_request(payload: Any) -> tuple[Optional[str], dict[str, Any]]:
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


def _extract_question_option_label(option: Any) -> Optional[str]:
    if isinstance(option, str):
        return option.strip() or None
    if isinstance(option, dict):
        for key in ("label", "text", "value", "name", "id"):
            value = option.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_question_options(question: dict[str, Any]) -> list[str]:
    for key in ("options", "choices"):
        raw = question.get(key)
        if isinstance(raw, list):
            options = []
            for option in raw:
                label = _extract_question_option_label(option)
                if label:
                    options.append(label)
            return options
    return []


def _auto_answers_for_questions(
    questions: list[dict[str, Any]], policy: str
) -> list[list[str]]:
    if policy == "auto_unanswered":
        return [[] for _ in questions]
    answers: list[list[str]] = []
    for question in questions:
        options = _extract_question_options(question)
        if options:
            answers.append([options[0]])
        else:
            answers.append([])
    return answers


def _normalize_question_answers(
    answers: Any, *, question_count: int
) -> list[list[str]]:
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


def _summarize_question_answers(answers: list[list[str]]) -> list[str]:
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


def _normalize_permission_decision(decision: Any) -> str:
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


def _permission_policy_reply(policy: str) -> str:
    if policy == PERMISSION_ALLOW:
        return OPENCODE_PERMISSION_ONCE
    return OPENCODE_PERMISSION_REJECT


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _extract_usage_field(
    payload: dict[str, Any], keys: tuple[str, ...]
) -> Optional[int]:
    for key in keys:
        if key in payload:
            value = _coerce_int(payload.get(key))
            if value is not None:
                return value
    return None


def _flatten_opencode_tokens(tokens: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage: dict[str, Any] = {}
    total_tokens = _coerce_int(tokens.get("total"))
    if total_tokens is not None:
        usage["totalTokens"] = total_tokens
    input_tokens = _coerce_int(tokens.get("input"))
    if input_tokens is not None:
        usage["inputTokens"] = input_tokens
    output_tokens = _coerce_int(tokens.get("output"))
    if output_tokens is not None:
        usage["outputTokens"] = output_tokens
    reasoning_tokens = _coerce_int(tokens.get("reasoning"))
    if reasoning_tokens is not None:
        usage["reasoningTokens"] = reasoning_tokens
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        cached_read = _coerce_int(cache.get("read"))
        if cached_read is not None:
            usage["cachedInputTokens"] = cached_read
        cached_write = _coerce_int(cache.get("write"))
        if cached_write is not None:
            usage["cacheWriteTokens"] = cached_write
    if "totalTokens" not in usage:
        components = [
            usage.get("inputTokens"),
            usage.get("outputTokens"),
            usage.get("reasoningTokens"),
            usage.get("cachedInputTokens"),
            usage.get("cacheWriteTokens"),
        ]
        numeric = [value for value in components if isinstance(value, int)]
        if numeric:
            usage["totalTokens"] = sum(numeric)
    return usage or None


def _extract_usage_payload(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    info = payload.get("info")
    if isinstance(info, dict):
        containers.append(info)
    properties = payload.get("properties")
    if isinstance(properties, dict):
        containers.append(properties)
        prop_info = properties.get("info")
        if isinstance(prop_info, dict):
            containers.append(prop_info)
    response = payload.get("response")
    if isinstance(response, dict):
        containers.append(response)
    for container in containers:
        for key in (
            "usage",
            "token_usage",
            "tokenUsage",
            "usage_stats",
            "usageStats",
            "stats",
        ):
            usage = container.get(key)
            if isinstance(usage, dict):
                return usage
        tokens = container.get("tokens")
        if isinstance(tokens, dict):
            flattened = _flatten_opencode_tokens(tokens)
            if flattened:
                return flattened
    return None


def _extract_total_tokens(usage: dict[str, Any]) -> Optional[int]:
    total = _extract_usage_field(usage, _OPENCODE_USAGE_TOTAL_KEYS)
    if total is not None:
        return total
    input_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_INPUT_KEYS) or 0
    cached_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_CACHED_KEYS) or 0
    output_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_OUTPUT_KEYS) or 0
    reasoning_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_REASONING_KEYS) or 0
    if input_tokens or cached_tokens or output_tokens or reasoning_tokens:
        return input_tokens + cached_tokens + output_tokens + reasoning_tokens
    return None


def _extract_usage_details(usage: dict[str, Any]) -> dict[str, int]:
    details: dict[str, int] = {}
    input_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_INPUT_KEYS)
    if input_tokens is not None:
        details["inputTokens"] = input_tokens
    cached_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_CACHED_KEYS)
    if cached_tokens is not None:
        details["cachedInputTokens"] = cached_tokens
    output_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_OUTPUT_KEYS)
    if output_tokens is not None:
        details["outputTokens"] = output_tokens
    reasoning_tokens = _extract_usage_field(usage, _OPENCODE_USAGE_REASONING_KEYS)
    if reasoning_tokens is not None:
        details["reasoningTokens"] = reasoning_tokens
    return details


def _extract_context_window(
    payload: Any, usage: Optional[dict[str, Any]]
) -> Optional[int]:
    containers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        containers.append(payload)
        info = payload.get("info")
        if isinstance(info, dict):
            containers.append(info)
        properties = payload.get("properties")
        if isinstance(properties, dict):
            containers.append(properties)
            prop_info = properties.get("info")
            if isinstance(prop_info, dict):
                containers.append(prop_info)
        response = payload.get("response")
        if isinstance(response, dict):
            containers.append(response)
            response_info = response.get("info")
            if isinstance(response_info, dict):
                containers.append(response_info)
            response_props = response.get("properties")
            if isinstance(response_props, dict):
                containers.append(response_props)
                response_prop_info = response_props.get("info")
                if isinstance(response_prop_info, dict):
                    containers.append(response_prop_info)
        for key in ("model", "modelInfo", "model_info", "modelConfig", "model_config"):
            model = payload.get(key)
            if isinstance(model, dict):
                containers.append(model)
    if isinstance(usage, dict):
        containers.insert(0, usage)
    for container in containers:
        for key in _OPENCODE_CONTEXT_WINDOW_KEYS:
            value = _coerce_int(container.get(key))
            if value is not None and value > 0:
                return value
    return None


def _extract_status_type(payload: Any) -> Optional[str]:
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
            status = container.get("status")
        else:
            status = container
        if isinstance(status, dict):
            value = status.get("type") or status.get("status")
        else:
            value = status
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        status = properties.get("status")
        if isinstance(status, dict):
            value = status.get("type") or status.get("status")
            if isinstance(value, str) and value:
                return value
    return None


def _status_is_idle(status_type: Optional[str]) -> bool:
    if not status_type:
        return False
    return status_type.strip().lower() in _OPENCODE_IDLE_STATUS_VALUES


async def opencode_missing_env(
    client: Any,
    workspace_root: str,
    model_payload: Optional[dict[str, str]],
    *,
    env: Optional[MutableMapping[str, str]] = None,
) -> list[str]:
    if not model_payload:
        return []
    provider_id = model_payload.get("providerID")
    if not provider_id:
        return []
    try:
        payload = await client.providers(directory=workspace_root)
    except Exception:
        return []
    providers: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_providers = payload.get("providers")
        if isinstance(raw_providers, list):
            providers = [entry for entry in raw_providers if isinstance(entry, dict)]
    elif isinstance(payload, list):
        providers = [entry for entry in payload if isinstance(entry, dict)]
    for provider in providers:
        pid = provider.get("id") or provider.get("providerID")
        if not pid or pid != provider_id:
            continue
        if _provider_has_auth(pid, workspace_root):
            return []
        env_keys = provider.get("env")
        if not isinstance(env_keys, list):
            return []
        missing = [
            key
            for key in env_keys
            if isinstance(key, str) and key and not _get_env_value(key, env)
        ]
        return missing
    return []


def _get_env_value(
    key: str, env: Optional[MutableMapping[str, str]] = None
) -> Optional[str]:
    if env is not None:
        return env.get(key)
    return os.getenv(key)


def _provider_has_auth(provider_id: str, workspace_root: str) -> bool:
    auth_path = _find_opencode_auth_path(workspace_root)
    if auth_path is None or not auth_path.exists():
        return False
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    entry = payload.get(provider_id)
    return isinstance(entry, dict) and any(bool(value) for value in entry.values())


def _find_opencode_auth_path(workspace_root: str) -> Optional[Path]:
    data_home = os.getenv("XDG_DATA_HOME")
    if not data_home:
        home = os.getenv("HOME")
        if not home:
            inferred = infer_home_from_workspace(workspace_root)
            if inferred is None:
                return None
            data_home = str(inferred / ".local" / "share")
        else:
            data_home = str(Path(home) / ".local" / "share")
    return Path(data_home) / "opencode" / "auth.json"


async def collect_opencode_output_from_events(
    events: Optional[AsyncIterator[SSEEvent]] = None,
    *,
    session_id: str,
    model_payload: Optional[dict[str, str]] = None,
    progress_session_ids: Optional[set[str]] = None,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    question_policy: str = "ignore",
    question_handler: Optional[QuestionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    respond_permission: Optional[Callable[[str, str], Awaitable[None]]] = None,
    reply_question: Optional[Callable[[str, list[list[str]]], Awaitable[None]]] = None,
    reject_question: Optional[Callable[[str], Awaitable[None]]] = None,
    part_handler: Optional[PartHandler] = None,
    event_stream_factory: Optional[Callable[[], AsyncIterator[SSEEvent]]] = None,
    session_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
    provider_fetcher: Optional[Callable[[], Awaitable[Any]]] = None,
    stall_timeout_seconds: Optional[float] = _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS,
) -> OpenCodeTurnOutput:
    text_parts: list[str] = []
    part_lengths: dict[str, int] = {}
    last_full_text = ""
    error: Optional[str] = None
    message_roles: dict[str, str] = {}
    message_roles_seen = False
    pending_text: dict[str, list[str]] = {}
    pending_no_id: list[str] = []
    no_id_role: Optional[str] = None
    fallback_message: Optional[tuple[Optional[str], Optional[str], str]] = None
    last_usage_total: Optional[int] = None
    last_context_window: Optional[int] = None
    part_types: dict[str, str] = {}
    seen_question_request_ids: set[tuple[Optional[str], str]] = set()
    logged_permission_errors: set[str] = set()
    normalized_question_policy = _normalize_question_policy(question_policy)
    logger = logging.getLogger(__name__)
    providers_cache: Optional[list[dict[str, Any]]] = None
    context_window_cache: dict[str, Optional[int]] = {}
    session_model_ids: Optional[tuple[Optional[str], Optional[str]]] = None
    default_model_ids = (
        _extract_model_ids(model_payload) if isinstance(model_payload, dict) else None
    )

    def _message_id_from_info(info: Any) -> Optional[str]:
        if not isinstance(info, dict):
            return None
        for key in ("id", "messageID", "messageId", "message_id"):
            value = info.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _message_id_from_part(part: Any) -> Optional[str]:
        if not isinstance(part, dict):
            return None
        for key in ("messageID", "messageId", "message_id"):
            value = part.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _register_message_role(payload: Any) -> tuple[Optional[str], Optional[str]]:
        nonlocal message_roles_seen
        if not isinstance(payload, dict):
            return None, None
        info = payload.get("info")
        if not isinstance(info, dict):
            properties = payload.get("properties")
            if isinstance(properties, dict):
                info = properties.get("info")
        role = info.get("role") if isinstance(info, dict) else None
        msg_id = _message_id_from_info(info)
        if isinstance(role, str) and msg_id:
            message_roles[msg_id] = role
            message_roles_seen = True
        return msg_id, role if isinstance(role, str) else None

    def _flush_pending_no_id_as_assistant() -> None:
        nonlocal no_id_role
        if pending_no_id:
            text_parts.extend(pending_no_id)
            pending_no_id.clear()
        no_id_role = "assistant"

    def _discard_pending_no_id() -> None:
        if pending_no_id:
            pending_no_id.clear()

    def _append_text_for_message(message_id: Optional[str], text: str) -> None:
        if not text:
            return
        if message_id is None:
            if no_id_role == "assistant":
                text_parts.append(text)
            else:
                pending_no_id.append(text)
            return
        role = message_roles.get(message_id)
        if role == "user":
            return
        if role == "assistant":
            text_parts.append(text)
            return
        pending_text.setdefault(message_id, []).append(text)

    def _flush_pending_text(message_id: Optional[str]) -> None:
        if not message_id:
            return
        role = message_roles.get(message_id)
        if role != "assistant":
            pending_text.pop(message_id, None)
            return
        pending = pending_text.pop(message_id, [])
        if pending:
            text_parts.extend(pending)

    def _flush_all_pending_text() -> None:
        if pending_text:
            for pending in list(pending_text.values()):
                if pending:
                    text_parts.extend(pending)
            pending_text.clear()
        if pending_no_id:
            # If we have not seen a role yet, assume assistant for backwards
            # compatibility with providers that omit roles entirely. Otherwise,
            # only flush when we have already classified no-id text as assistant
            # or when we have no other text (to avoid echoing user prompts).
            if not message_roles_seen or no_id_role == "assistant" or not text_parts:
                text_parts.extend(pending_no_id)
            pending_no_id.clear()

    def _handle_role_update(message_id: Optional[str], role: Optional[str]) -> None:
        nonlocal no_id_role
        if not role:
            return
        if role == "assistant":
            _flush_pending_text(message_id)
            _flush_pending_no_id_as_assistant()
            return
        if role == "user":
            _flush_pending_text(message_id)
            _discard_pending_no_id()
            no_id_role = None

    async def _resolve_session_model_ids() -> tuple[Optional[str], Optional[str]]:
        nonlocal session_model_ids
        if session_model_ids is not None:
            return session_model_ids
        resolved_ids: Optional[tuple[Optional[str], Optional[str]]] = None
        if session_fetcher is not None:
            try:
                payload = await session_fetcher()
                resolved_ids = _extract_model_ids(payload)
            except Exception:
                resolved_ids = None
        # If we failed to resolve model ids from the session (including the empty
        # tuple case), fall back to the caller-provided model payload so we can
        # still backfill usage metadata.
        if not resolved_ids or all(value is None for value in resolved_ids):
            resolved_ids = default_model_ids
        session_model_ids = resolved_ids or (None, None)
        return session_model_ids

    async def _resolve_context_window_from_providers(
        provider_id: Optional[str], model_id: Optional[str]
    ) -> Optional[int]:
        nonlocal providers_cache
        if not provider_id or not model_id:
            return None
        cache_key = f"{provider_id}/{model_id}"
        if cache_key in context_window_cache:
            return context_window_cache[cache_key]
        if provider_fetcher is None:
            context_window_cache[cache_key] = None
            return None
        if providers_cache is None:
            try:
                payload = await provider_fetcher()
            except Exception:
                context_window_cache[cache_key] = None
                return None
            providers: list[dict[str, Any]] = []
            if isinstance(payload, dict):
                raw_providers = payload.get("providers")
                if isinstance(raw_providers, list):
                    providers = [
                        entry for entry in raw_providers if isinstance(entry, dict)
                    ]
            elif isinstance(payload, list):
                providers = [entry for entry in payload if isinstance(entry, dict)]
            providers_cache = providers
        context_window = None
        for provider in providers_cache or []:
            pid = provider.get("id") or provider.get("providerID")
            if pid != provider_id:
                continue
            models = provider.get("models")
            model_entry = None
            if isinstance(models, dict):
                candidate = models.get(model_id)
                if isinstance(candidate, dict):
                    model_entry = candidate
            elif isinstance(models, list):
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = entry.get("id") or entry.get("modelID")
                    if entry_id == model_id:
                        model_entry = entry
                        break
            if isinstance(model_entry, dict):
                limit = model_entry.get("limit") or model_entry.get("limits")
                if isinstance(limit, dict):
                    for key in _OPENCODE_MODEL_CONTEXT_KEYS:
                        value = _coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
                if context_window is None:
                    for key in _OPENCODE_MODEL_CONTEXT_KEYS:
                        value = _coerce_int(model_entry.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            if context_window is None:
                limit = provider.get("limit") or provider.get("limits")
                if isinstance(limit, dict):
                    for key in _OPENCODE_MODEL_CONTEXT_KEYS:
                        value = _coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            break
        context_window_cache[cache_key] = context_window
        return context_window

    stream_factory = event_stream_factory
    if events is None and stream_factory is None:
        raise ValueError("events or event_stream_factory must be provided")

    def _new_stream() -> AsyncIterator[SSEEvent]:
        if stream_factory is not None:
            return stream_factory()
        if events is None:
            raise ValueError("events or event_stream_factory must be provided")
        return events

    async def _close_stream(iterator: AsyncIterator[SSEEvent]) -> None:
        aclose = getattr(iterator, "aclose", None)
        if aclose is None:
            return
        with suppress(Exception):
            await aclose()

    stream_iter = _new_stream().__aiter__()
    last_relevant_event_at = time.monotonic()
    last_primary_completion_at: Optional[float] = None
    reconnect_attempts = 0
    can_reconnect = (
        event_stream_factory is not None and stall_timeout_seconds is not None
    )

    try:
        while True:
            if should_stop is not None and should_stop():
                break
            try:
                if can_reconnect and stall_timeout_seconds is not None:
                    event = await asyncio.wait_for(
                        stream_iter.__anext__(), timeout=stall_timeout_seconds
                    )
                else:
                    event = await stream_iter.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                status_type = None
                if session_fetcher is not None:
                    try:
                        payload = await session_fetcher()
                        status_type = _extract_status_type(payload)
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "opencode.session.poll_failed",
                            session_id=session_id,
                            exc=exc,
                        )
                idle_seconds = now - last_relevant_event_at
                if _status_is_idle(status_type):
                    log_event(
                        logger,
                        logging.INFO,
                        "opencode.stream.stalled.session_idle",
                        session_id=session_id,
                        status_type=status_type,
                        idle_seconds=idle_seconds,
                    )
                    if not text_parts and (pending_text or pending_no_id):
                        _flush_all_pending_text()
                    break
                if last_primary_completion_at is not None:
                    log_event(
                        logger,
                        logging.INFO,
                        "opencode.stream.stalled.after_completion",
                        session_id=session_id,
                        status_type=status_type,
                        idle_seconds=idle_seconds,
                    )
                if not can_reconnect:
                    break
                backoff_index = min(
                    reconnect_attempts,
                    len(_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS) - 1,
                )
                backoff = _OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS[backoff_index]
                reconnect_attempts += 1
                log_event(
                    logger,
                    logging.WARNING,
                    "opencode.stream.stalled.reconnecting",
                    session_id=session_id,
                    idle_seconds=idle_seconds,
                    backoff_seconds=backoff,
                    status_type=status_type,
                    attempts=reconnect_attempts,
                )
                await _close_stream(stream_iter)
                await asyncio.sleep(backoff)
                stream_iter = _new_stream().__aiter__()
                last_relevant_event_at = now
                continue
            now = time.monotonic()
            raw = event.data or ""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            event_session_id = extract_session_id(payload)
            is_relevant = False
            if event_session_id:
                if progress_session_ids is None:
                    is_relevant = event_session_id == session_id
                else:
                    is_relevant = event_session_id in progress_session_ids
            if not is_relevant:
                if (
                    stall_timeout_seconds is not None
                    and now - last_relevant_event_at > stall_timeout_seconds
                ):
                    idle_seconds = now - last_relevant_event_at
                    last_relevant_event_at = now
                    status_type = None
                    if session_fetcher is not None:
                        try:
                            payload = await session_fetcher()
                            status_type = _extract_status_type(payload)
                        except Exception as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "opencode.session.poll_failed",
                                session_id=session_id,
                                exc=exc,
                            )
                    if _status_is_idle(status_type):
                        log_event(
                            logger,
                            logging.INFO,
                            "opencode.stream.stalled.session_idle",
                            session_id=session_id,
                            status_type=status_type,
                            idle_seconds=idle_seconds,
                        )
                        if not text_parts and (pending_text or pending_no_id):
                            _flush_all_pending_text()
                        break
                    if last_primary_completion_at is not None:
                        log_event(
                            logger,
                            logging.INFO,
                            "opencode.stream.stalled.after_completion",
                            session_id=session_id,
                            status_type=status_type,
                            idle_seconds=idle_seconds,
                        )
                    if not can_reconnect:
                        break
                    backoff_index = min(
                        reconnect_attempts,
                        len(_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS) - 1,
                    )
                    backoff = _OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS[backoff_index]
                    reconnect_attempts += 1
                    log_event(
                        logger,
                        logging.WARNING,
                        "opencode.stream.stalled.reconnecting",
                        session_id=session_id,
                        idle_seconds=idle_seconds,
                        backoff_seconds=backoff,
                        status_type=status_type,
                        attempts=reconnect_attempts,
                    )
                    await _close_stream(stream_iter)
                    await asyncio.sleep(backoff)
                    stream_iter = _new_stream().__aiter__()
                continue
            last_relevant_event_at = now
            reconnect_attempts = 0
            is_primary_session = event_session_id == session_id
            if event.event == "question.asked":
                request_id, props = _extract_question_request(payload)
                questions = props.get("questions") if isinstance(props, dict) else []
                question_count = len(questions) if isinstance(questions, list) else 0
                log_event(
                    logger,
                    logging.INFO,
                    "opencode.question.asked",
                    request_id=request_id,
                    question_count=question_count,
                    session_id=event_session_id,
                )
                if not request_id:
                    continue
                dedupe_key = (event_session_id, request_id)
                if dedupe_key in seen_question_request_ids:
                    continue
                seen_question_request_ids.add(dedupe_key)
                if question_handler is not None:
                    try:
                        answers = await question_handler(request_id, props)
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "opencode.question.auto_reply_failed",
                            request_id=request_id,
                            session_id=event_session_id,
                            exc=exc,
                        )
                        if reject_question is not None:
                            try:
                                await reject_question(request_id)
                            except Exception:
                                pass
                        continue
                    if answers is None:
                        if reject_question is not None:
                            try:
                                await reject_question(request_id)
                            except Exception:
                                pass
                        continue
                    normalized_answers = _normalize_question_answers(
                        answers, question_count=question_count
                    )
                    if reply_question is not None:
                        try:
                            await reply_question(request_id, normalized_answers)
                            log_event(
                                logger,
                                logging.INFO,
                                "opencode.question.replied",
                                request_id=request_id,
                                question_count=question_count,
                                session_id=event_session_id,
                                mode="handler",
                            )
                        except Exception as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "opencode.question.auto_reply_failed",
                                request_id=request_id,
                                session_id=event_session_id,
                                exc=exc,
                            )
                    continue
                if normalized_question_policy == "ignore":
                    continue
                if normalized_question_policy == "reject":
                    if reject_question is not None:
                        try:
                            await reject_question(request_id)
                        except Exception as exc:
                            log_event(
                                logger,
                                logging.WARNING,
                                "opencode.question.auto_reply_failed",
                                request_id=request_id,
                                session_id=event_session_id,
                                exc=exc,
                            )
                    continue
                auto_answers = _auto_answers_for_questions(
                    questions if isinstance(questions, list) else [],
                    normalized_question_policy,
                )
                normalized_answers = _normalize_question_answers(
                    auto_answers, question_count=question_count
                )
                if reply_question is not None:
                    try:
                        await reply_question(request_id, normalized_answers)
                        log_event(
                            logger,
                            logging.INFO,
                            "opencode.question.auto_replied",
                            request_id=request_id,
                            question_count=question_count,
                            session_id=event_session_id,
                            policy=normalized_question_policy,
                            answers=_summarize_question_answers(normalized_answers),
                        )
                    except Exception as exc:
                        log_event(
                            logger,
                            logging.WARNING,
                            "opencode.question.auto_reply_failed",
                            request_id=request_id,
                            session_id=event_session_id,
                            exc=exc,
                        )
                continue
            if event.event == "permission.asked":
                request_id, props = _extract_permission_request(payload)
                if request_id and respond_permission is not None:
                    if (
                        permission_policy == PERMISSION_ASK
                        and permission_handler is not None
                    ):
                        try:
                            decision = await permission_handler(request_id, props)
                        except Exception:
                            decision = OPENCODE_PERMISSION_REJECT
                        reply = _normalize_permission_decision(decision)
                    else:
                        reply = _permission_policy_reply(permission_policy)
                    try:
                        await respond_permission(request_id, reply)
                    except Exception as exc:
                        status_code = None
                        body_preview = None
                        if isinstance(exc, httpx.HTTPStatusError):
                            status_code = exc.response.status_code
                            body_preview = (exc.response.text or "").strip()[
                                :200
                            ] or None
                            if (
                                status_code is not None
                                and 400 <= status_code < 500
                                and request_id not in logged_permission_errors
                            ):
                                logged_permission_errors.add(request_id)
                                log_event(
                                    logger,
                                    logging.ERROR,
                                    "opencode.permission.reply_failed",
                                    request_id=request_id,
                                    reply=reply,
                                    status_code=status_code,
                                    body_preview=body_preview,
                                    session_id=event_session_id,
                                )
                        else:
                            log_event(
                                logger,
                                logging.ERROR,
                                "opencode.permission.reply_failed",
                                request_id=request_id,
                                reply=reply,
                                session_id=event_session_id,
                                exc=exc,
                            )
                        if is_primary_session:
                            detail = body_preview or _extract_error_text(payload)
                            error = "OpenCode permission reply failed"
                            if status_code is not None:
                                error = f"{error} ({status_code})"
                            if detail:
                                error = f"{error}: {detail}"
                            break
            if event.event == "session.error":
                if is_primary_session:
                    error = _extract_error_text(payload) or "OpenCode session error"
                    break
                continue
            if event.event in ("message.updated", "message.completed"):
                if is_primary_session:
                    msg_id, role = _register_message_role(payload)
                    _handle_role_update(msg_id, role)
            if event.event == "message.part.updated":
                properties = (
                    payload.get("properties") if isinstance(payload, dict) else None
                )
                if isinstance(properties, dict):
                    part = properties.get("part")
                    delta = properties.get("delta")
                else:
                    part = payload.get("part")
                    delta = payload.get("delta")
                part_dict = part if isinstance(part, dict) else None
                part_with_session = None
                if isinstance(part_dict, dict):
                    part_with_session = dict(part_dict)
                    part_with_session["sessionID"] = event_session_id
                part_type = part_dict.get("type") if part_dict else None
                part_ignored = bool(part_dict.get("ignored")) if part_dict else False
                part_message_id = _message_id_from_part(part_dict)
                part_id = None
                if part_dict:
                    part_id = part_dict.get("id") or part_dict.get("partId")
                    if (
                        isinstance(part_id, str)
                        and part_id
                        and isinstance(part_type, str)
                    ):
                        part_types[part_id] = part_type
                    elif (
                        isinstance(part_id, str)
                        and part_id
                        and not isinstance(part_type, str)
                        and part_id in part_types
                    ):
                        part_type = part_types[part_id]
                if isinstance(delta, dict):
                    delta_text = delta.get("text")
                elif isinstance(delta, str):
                    delta_text = delta
                else:
                    delta_text = None
                if isinstance(delta_text, str) and delta_text:
                    if part_type == "reasoning":
                        if part_handler and part_dict:
                            await part_handler(
                                "reasoning", part_with_session or part_dict, delta_text
                            )
                    elif part_type in (None, "text") and not part_ignored:
                        if not is_primary_session:
                            continue
                        _append_text_for_message(part_message_id, delta_text)
                        # Update dedupe bookkeeping for text deltas to prevent re-adding later
                        if isinstance(part_dict, dict):
                            part_id = part_dict.get("id") or part_dict.get("partId")
                            text = part_dict.get("text")
                            if (
                                isinstance(part_id, str)
                                and part_id
                                and isinstance(text, str)
                            ):
                                part_lengths[part_id] = len(text)
                            elif isinstance(text, str):
                                last_full_text = text
                        if part_handler and part_dict:
                            await part_handler(
                                "text", part_with_session or part_dict, delta_text
                            )
                    elif part_handler and part_dict and part_type:
                        await part_handler(
                            part_type, part_with_session or part_dict, delta_text
                        )
                elif (
                    isinstance(part_dict, dict)
                    and part_type in (None, "text")
                    and not part_ignored
                ):
                    if not is_primary_session:
                        continue
                    text = part_dict.get("text")
                    if isinstance(text, str) and text:
                        part_id = part_dict.get("id") or part_dict.get("partId")
                        if isinstance(part_id, str) and part_id:
                            last_len = part_lengths.get(part_id, 0)
                            if len(text) > last_len:
                                _append_text_for_message(
                                    part_message_id, text[last_len:]
                                )
                                part_lengths[part_id] = len(text)
                        else:
                            if last_full_text and text.startswith(last_full_text):
                                _append_text_for_message(
                                    part_message_id, text[len(last_full_text) :]
                                )
                            elif text != last_full_text:
                                _append_text_for_message(part_message_id, text)
                            last_full_text = text
                elif part_handler and part_dict and part_type:
                    await part_handler(part_type, part_with_session or part_dict, None)
            if event.event in ("message.completed", "message.updated"):
                message_result = parse_message_response(payload)
                msg_id = None
                role = None
                if is_primary_session:
                    msg_id, role = _register_message_role(payload)
                    resolved_role = role
                    if resolved_role is None and msg_id:
                        resolved_role = message_roles.get(msg_id)
                    if message_result.text:
                        if resolved_role == "assistant" or resolved_role is None:
                            fallback_message = (
                                msg_id,
                                resolved_role,
                                message_result.text,
                            )
                            if resolved_role is None:
                                log_event(
                                    logger,
                                    logging.DEBUG,
                                    "opencode.message.completed.role_missing",
                                    session_id=event_session_id,
                                    message_id=msg_id,
                                )
                        else:
                            log_event(
                                logger,
                                logging.DEBUG,
                                "opencode.message.completed.ignored",
                                session_id=event_session_id,
                                message_id=msg_id,
                                role=resolved_role,
                            )
                    if message_result.error and not error:
                        error = message_result.error
                if part_handler is not None and is_primary_session:
                    usage = _extract_usage_payload(payload)
                    if usage is not None:
                        provider_id, model_id = _extract_model_ids(payload)
                        if not provider_id or not model_id:
                            provider_id, model_id = await _resolve_session_model_ids()
                        total_tokens = _extract_total_tokens(usage)
                        context_window = _extract_context_window(payload, usage)
                        if context_window is None:
                            context_window = (
                                await _resolve_context_window_from_providers(
                                    provider_id, model_id
                                )
                            )
                        usage_details = _extract_usage_details(usage)
                        if (
                            total_tokens != last_usage_total
                            or context_window != last_context_window
                        ):
                            last_usage_total = total_tokens
                            last_context_window = context_window
                            usage_snapshot: dict[str, Any] = {}
                            if provider_id:
                                usage_snapshot["providerID"] = provider_id
                            if model_id:
                                usage_snapshot["modelID"] = model_id
                            if total_tokens is not None:
                                usage_snapshot["totalTokens"] = total_tokens
                            if usage_details:
                                usage_snapshot.update(usage_details)
                            if context_window is not None:
                                usage_snapshot["modelContextWindow"] = context_window
                            if usage_snapshot:
                                await part_handler("usage", usage_snapshot, None)
            if event.event == "session.idle" or (
                event.event == "session.status"
                and _status_is_idle(_extract_status_type(payload))
            ):
                if not is_primary_session:
                    continue
                if not text_parts and (pending_text or pending_no_id):
                    _flush_all_pending_text()
                break
            if event.event == "message.completed" and is_primary_session:
                last_primary_completion_at = time.monotonic()
    finally:
        await _close_stream(stream_iter)

    if not text_parts and fallback_message is not None:
        msg_id, role, text = fallback_message
        resolved_role = role
        if resolved_role is None and msg_id:
            resolved_role = message_roles.get(msg_id)
        if resolved_role == "assistant":
            _append_text_for_message(msg_id, text)
            if pending_text or pending_no_id:
                _flush_all_pending_text()

    return OpenCodeTurnOutput(text="".join(text_parts).strip(), error=error)


async def collect_opencode_output(
    client: Any,
    *,
    session_id: str,
    workspace_path: str,
    model_payload: Optional[dict[str, str]] = None,
    progress_session_ids: Optional[set[str]] = None,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    question_policy: str = "ignore",
    question_handler: Optional[QuestionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    ready_event: Optional[Any] = None,
    part_handler: Optional[PartHandler] = None,
    stall_timeout_seconds: Optional[float] = _OPENCODE_STREAM_STALL_TIMEOUT_SECONDS,
) -> OpenCodeTurnOutput:
    async def _respond(request_id: str, reply: str) -> None:
        await client.respond_permission(request_id=request_id, reply=reply)

    async def _reply_question(request_id: str, answers: list[list[str]]) -> None:
        await client.reply_question(request_id, answers=answers)

    async def _reject_question(request_id: str) -> None:
        await client.reject_question(request_id)

    def _stream_factory() -> AsyncIterator[SSEEvent]:
        return cast(
            AsyncIterator[SSEEvent],
            client.stream_events(directory=workspace_path, ready_event=ready_event),
        )

    async def _fetch_session() -> Any:
        statuses = await client.session_status(directory=workspace_path)
        if isinstance(statuses, dict):
            session_status = statuses.get(session_id)
            if session_status is None:
                return {"status": {"type": "idle"}}
            if isinstance(session_status, dict):
                return {"status": session_status}
            if isinstance(session_status, str):
                return {"status": session_status}
        return {"status": {}}

    async def _fetch_providers() -> Any:
        return await client.providers(directory=workspace_path)

    return await collect_opencode_output_from_events(
        None,
        session_id=session_id,
        progress_session_ids=progress_session_ids,
        permission_policy=permission_policy,
        permission_handler=permission_handler,
        question_policy=question_policy,
        question_handler=question_handler,
        should_stop=should_stop,
        respond_permission=_respond,
        reply_question=_reply_question,
        reject_question=_reject_question,
        part_handler=part_handler,
        event_stream_factory=_stream_factory,
        model_payload=model_payload,
        session_fetcher=_fetch_session,
        provider_fetcher=_fetch_providers,
        stall_timeout_seconds=stall_timeout_seconds,
    )


__all__ = [
    "OpenCodeMessageResult",
    "OpenCodeTurnOutput",
    "PERMISSION_ALLOW",
    "PERMISSION_ASK",
    "PERMISSION_DENY",
    "build_turn_id",
    "collect_opencode_output",
    "collect_opencode_output_from_events",
    "extract_session_id",
    "extract_turn_id",
    "format_permission_prompt",
    "map_approval_policy_to_permission",
    "opencode_missing_env",
    "parse_message_response",
    "PartHandler",
    "QuestionHandler",
    "split_model_id",
]
