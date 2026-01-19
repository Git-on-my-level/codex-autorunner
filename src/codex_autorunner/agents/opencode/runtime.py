from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    MutableMapping,
    Optional,
)

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
        value = properties.get("sessionID")
        if isinstance(value, str) and value:
            return value
        part = properties.get("part")
        if isinstance(part, dict):
            value = part.get("sessionID")
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return extract_session_id(session, allow_fallback_id=allow_fallback_id)
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
        if pid != provider_id:
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
    events: AsyncIterator[SSEEvent],
    *,
    session_id: str,
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
) -> OpenCodeTurnOutput:
    text_parts: list[str] = []
    part_lengths: dict[str, int] = {}
    last_full_text = ""
    error: Optional[str] = None
    message_roles: dict[str, str] = {}
    message_roles_seen = False
    last_role_seen: Optional[str] = None
    pending_text: dict[str, list[str]] = {}
    last_usage_total: Optional[int] = None
    last_context_window: Optional[int] = None
    seen_question_request_ids: set[tuple[str, str]] = set()
    normalized_question_policy = _normalize_question_policy(question_policy)
    logger = logging.getLogger(__name__)

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
        nonlocal last_role_seen, message_roles_seen
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
            last_role_seen = role
        return msg_id, role if isinstance(role, str) else None

    def _append_text_for_message(message_id: Optional[str], text: str) -> None:
        if not text:
            return
        if message_id is None:
            if not message_roles_seen:
                text_parts.append(text)
                return
            if last_role_seen != "user":
                text_parts.append(text)
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
        if not pending_text:
            return
        for pending in list(pending_text.values()):
            if pending:
                text_parts.extend(pending)
        pending_text.clear()

    async for event in events:
        if should_stop is not None and should_stop():
            break
        raw = event.data or ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        event_session_id = extract_session_id(payload)
        if not event_session_id:
            continue
        if progress_session_ids is None:
            if event_session_id != session_id:
                continue
        elif event_session_id not in progress_session_ids:
            continue
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
                reply = PERMISSION_DENY
                if permission_policy == PERMISSION_ALLOW:
                    reply = PERMISSION_ALLOW
                elif (
                    permission_policy == PERMISSION_ASK
                    and permission_handler is not None
                ):
                    try:
                        decision = await permission_handler(request_id, props)
                    except Exception:
                        decision = "reject"
                    decision_norm = str(decision or "").strip().lower()
                    if decision_norm in ("allow", "approved", "approve"):
                        reply = PERMISSION_ALLOW
                    elif decision_norm in ("deny", "reject", "cancel"):
                        reply = PERMISSION_DENY
                try:
                    await respond_permission(request_id, reply)
                except Exception:
                    pass
        if event.event == "session.error":
            if is_primary_session:
                error = _extract_error_text(payload) or "OpenCode session error"
                break
            continue
        if event.event in ("message.updated", "message.completed"):
            if is_primary_session:
                msg_id, role = _register_message_role(payload)
                if role == "assistant":
                    _flush_pending_text(msg_id)
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
            if isinstance(delta, dict):
                delta_text = delta.get("text")
            elif isinstance(delta, str):
                delta_text = delta
            else:
                delta_text = None
            if isinstance(delta_text, str) and delta_text:
                if part_type == "text" and not part_ignored:
                    if not is_primary_session:
                        continue
                    _append_text_for_message(part_message_id, delta_text)
                elif part_handler and part_dict and part_type:
                    await part_handler(
                        part_type, part_with_session or part_dict, delta_text
                    )
            elif (
                isinstance(part_dict, dict) and part_type == "text" and not part_ignored
            ):
                if not is_primary_session:
                    continue
                text = part_dict.get("text")
                if isinstance(text, str) and text:
                    part_id = part_dict.get("id") or part_dict.get("partId")
                    if isinstance(part_id, str) and part_id:
                        last_len = part_lengths.get(part_id, 0)
                        if len(text) > last_len:
                            _append_text_for_message(part_message_id, text[last_len:])
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
                if message_result.text and not text_parts and role != "user":
                    _append_text_for_message(msg_id, message_result.text)
                if message_result.error and not error:
                    error = message_result.error
            if part_handler is not None:
                usage = _extract_usage_payload(payload)
                if usage is not None:
                    total_tokens = _extract_total_tokens(usage)
                    context_window = _extract_context_window(payload, usage)
                    usage_details = _extract_usage_details(usage)
                    if (
                        total_tokens != last_usage_total
                        or context_window != last_context_window
                    ):
                        last_usage_total = total_tokens
                        last_context_window = context_window
                        usage_snapshot: dict[str, Any] = {}
                        if total_tokens is not None:
                            usage_snapshot["totalTokens"] = total_tokens
                        if usage_details:
                            usage_snapshot.update(usage_details)
                        if context_window is not None:
                            usage_snapshot["modelContextWindow"] = context_window
                        if usage_snapshot:
                            await part_handler("usage", usage_snapshot, None)
        if event.event == "session.idle":
            if not is_primary_session:
                continue
            if not text_parts and pending_text:
                _flush_all_pending_text()
            break

    return OpenCodeTurnOutput(text="".join(text_parts).strip(), error=error)


async def collect_opencode_output(
    client: Any,
    *,
    session_id: str,
    workspace_path: str,
    progress_session_ids: Optional[set[str]] = None,
    permission_policy: str = PERMISSION_ALLOW,
    permission_handler: Optional[PermissionHandler] = None,
    question_policy: str = "ignore",
    question_handler: Optional[QuestionHandler] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    part_handler: Optional[PartHandler] = None,
) -> OpenCodeTurnOutput:
    async def _respond(request_id: str, reply: str) -> None:
        await client.respond_permission(request_id=request_id, reply=reply)

    async def _reply_question(request_id: str, answers: list[list[str]]) -> None:
        await client.reply_question(request_id, answers=answers)

    async def _reject_question(request_id: str) -> None:
        await client.reject_question(request_id)

    return await collect_opencode_output_from_events(
        client.stream_events(directory=workspace_path),
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
